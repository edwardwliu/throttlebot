import argparse
import paramiko
import re
from subprocess import Popen, PIPE
import sys
from time import sleep

import threading

from weighting_conversions import *
from remote_execution import *
from measure_utilization import *
from container_information import *
from max_resource_capacity import *


quilt_machines = ("quilt", "ps")

connected_pattern = re.compile(".+Connected}$")
ip_pattern = r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'

# Container blacklist for container names whose placements should not move
container_blacklist = ['ovn-controller', 'minion', 'ovs-vswitchd', 'ovsdb-server', 'etcd']

# Conversion factor from unit KEY to KiB
CONVERSION = {"KiB": 1, "MiB": 2**10, "GiB": 2**20}

# This is changed manually
MAX_NETWORK_BANDWIDTH = 600

'''Stressing the Network'''
# Container_to_bandwidth maps Docker container id to the bandwidth that container should be throttled to.
# Assumes that the container specified by container id is located in the machine for ssh_client
# Bandwidth units: bps
def set_egress_network_bandwidth(ssh_client, container_id, bandwidth):
    interface_name = get_container_veth(ssh_client, container_id)

    # Execute the command within OVS
    # OVS policy bandwidth accepts inputs in bps
    bandwidth_kbps = bandwidth / (10 ** 3)
    ovs_policy_cmd = 'ovs-vsctl set interface {} ingress_policing_rate={}'.format(interface_name, int(bandwidth_kbps))
    ovs_burst_cmd = 'ovs-vsctl set interface {} ingress_policing_burst={}'.format(interface_name, 0)
    docker_policing_cmd = "docker exec ovs-vswitchd {}".format(ovs_policy_cmd)
    docker_burst_cmd = "docker exec ovs-vswitchd {}".format(ovs_burst_cmd)

    print docker_policing_cmd

    #Should be no output if throttling is applied correctly
    _,_,err_val_rate = ssh_client.exec_command(docker_policing_cmd)
    _,_,err_val_burst = ssh_client.exec_command(docker_burst_cmd)
    if len(err_val_rate.readlines()) != 0:
        print 'ERROR: Stress of container id {} network failed'.format(container_id)
        print 'ERROR MESSAGE: {}'.format(err_val_rate)
        raise SystemError('Network Set Error')
    else:
        print 'SUCCESS: Network stress of container id {} stressed to {}'.format(container_id, bandwidth)
        return 1

def reset_egress_network_bandwidth(ssh_client, container_id):
    interface_name = get_container_veth(ssh_client, container_id)

    ovs_policy_cmd = 'ovs-vsctl set interface {} ingress_policing_rate={}'.format(interface_name, 0)
    reset_network_cmd = "docker exec ovs-vswitchd {}".format(ovs_policy_cmd)

    #Should be no output if throttling is applied correctly
    _,_,err_val_rate = ssh_client.exec_command(reset_network_cmd)
    if len(err_val_rate.readlines()) != 0:
        print 'ERROR: Resetting of container id {} network failed'.format(container_id)
        print 'ERROR MESSAGE: {}'.format(err_val_rate)
        raise SystemError('Network Set Error')
    else:
        print 'SUCCESS: Network stress of container id {} removed'.format(container_id)
        return 1

# Unused
# Removes all network manipulations for container_id (or ALL machines if specified) in the Quilt Environment
def remove_all_network_manipulation(ssh_client, container_id, remove_all_machines=False):
    all_machines = get_all_machines()
    if remove_all_machines is False:
        container_ids = get_container_id(ssh_client, full_id=False, append_c=True)
        for container in container_ids:
            container = 'ens3'
            cmd = "sudo tc qdisc del dev {} root".format(container)
            ssh_exec(ssh_client, cmd)
            return
        else:
            for machine in all_machines:
                temp_ssh_client = get_client(machine)
                container_ids = get_container_id(temp_ssh_client, full_id=False, append_c=False)
                for container in container_ids:
                    cmd = "sudo tc qdisc del dev {} root".format(container)
                    ssh_exec(temp_ssh_client, cmd)

'''Stressing the CPU'''
# Allows you to set the CPU periods in a container, intended for a container that is already running
# as opposed to update_cpu which actually just stresses the CPU by a predetermined amount
# Assumes that that CPU_period and CPU_quota were not set beforehand
# CPU Quota should be a percentage
# CPU period is assumed to be 1 second no matter what
def set_cpu_quota(ssh_client, container_id, cpu_period, cpu_quota_percent):
    cpu_quota = int((cpu_quota_percent/100.0) * cpu_period)
    #cpu_quota *= get_num_cores(ssh_client)

    print 'Adjusted CPU period: {}'.format(cpu_period)
    print 'CPU Quota: {}'.format(cpu_quota)

    throttled_containers = []
    
    update_command = 'docker update --cpu-period={} --cpu-quota={} {}'.format(cpu_period, cpu_quota, container_id)
    print update_command
    ssh_exec(ssh_client, update_command)
    throttled_containers.append(container_id)

    return throttled_containers

def reset_cpu_quota(ssh_client, container_id):
    # Reset only seems to work when both period and quota are high (and equal of course)
    update_command = 'docker update --cpu-quota=-1 {}'.format(container_id)
    print 'reset_cpu_quota'
    print update_command
    ssh_exec(ssh_client, update_command)


# Pins the selected cores to the container (will reset any CPU quotas)
# Hardcoded cpuset-mems for now
def set_cpu_cores(ssh_client, container_id, cores):
    cores = int(cores) - 1
    core_cmd = '0-{}'.format(cores)
    set_cores_cmd = 'docker update --cpuset-cpus={} --cpuset-mems=0 {}'.format(core_cmd, container_id)
    ssh_exec(ssh_client, set_cores_cmd)
    print '{} Cores pinned to container {}'.format(core_cmd, container_id)


# Resetting pinned cpu_cores (container will have access to all cores)
def reset_cpu_cores(ssh_client, container_id):
    cores = get_num_cores(ssh_client) - 1
    core_cmd = '0-{}'.format(cores)
    rst_cores_cmd = 'docker update --cpuset-cpus={} --cpuset-mems=0 {}'.format(core_cmd, container_id)
    ssh_exec(ssh_client, rst_cores_cmd)
    print 'Reset container {}\'s core restraints'.format(container_id)

'''Stressing the Disk Read/write throughput'''
# Positive value to set a maximum for both disk write and disk read
# 0 to reset the value
# Units are in MB/s
def change_container_blkio(ssh_client, container_id, disk_bandwidth):
    # Set Read and Write Conditions in real-time using cgroups
    # Assumes the other containers default to write to device major number 252 (minor number arbitrary)
    # Check for 202 or 252 for major device number
    set_cgroup_write_rate_cmd = 'echo "202:0 {}" | sudo tee /sys/fs/cgroup/blkio/docker/{}*/blkio.throttle.write_bps_device'.format(disk_bandwidth, container_id)
    set_cgroup_read_rate_cmd = 'echo "202:0 {}" | sudo tee /sys/fs/cgroup/blkio/docker/{}*/blkio.throttle.read_bps_device'.format(disk_bandwidth, container_id)

    print set_cgroup_write_rate_cmd
    print set_cgroup_read_rate_cmd

    ssh_exec(ssh_client, set_cgroup_write_rate_cmd)
    ssh_exec(ssh_client, set_cgroup_read_rate_cmd)

    # Sleep 1 seconds since the current queue must be emptied before this can be fulfilled
    sleep(1)

'''Helper functions that are used for various reasons'''

def convert_to_kib(mem):
    """MEM is a tuple (mem_size, unit). Unit must be KiB, MiB, or GiB.
    Returns mem_size converted to KiB."""
    mem_size, unit = mem
    multiplier = CONVERSION[unit]
    return mem_size * multiplier

def matches_ip(line, ip):
    m = re.search(ip_pattern, line)
    return m.group(0) == ip


def is_connected(line):
    line = line.strip()
    return connected_pattern.match(line)


def check_valid_ip(vm_ip):
    p = Popen(quilt_machines, stdout=PIPE)
    lines = p.stdout.read().splitlines()
    matched = False

    for line in lines:
        if not matches_ip(line, vm_ip):
            continue
        if not is_connected(line):
            sys.exit("The VM ({}) is not connected".format(vm_ip))
        matched = True
        break

    if not matched:
        sys.exit("There is no VM with public IP {}".format(vm_ip))

def get_all_machines():
    all_machines = []
    p = Popen(quilt_machines, stdout=PIPE)
    lines = p.stdout.read().splitlines()
    for line in lines:
        m  = re.search(ip_pattern, line)
        if m:
            m = m.group(0)
            all_machines.append(m)
    return set(all_machines)

# LEGACY
def initialize_machine(ssh_client):
    install_deps(ssh_client)

# LEGACY
def install_deps(ssh_client):
    cmd_cpulimit = "sudo DEBIAN_FRONTEND=noninteractive apt-get install cpulimit"
    cmd_stress = "sudo DEBIAN_FRONTEND=noninteractive apt-get install stress"
    ssh_exec(ssh_client, cmd_cpulimit)
    ssh_exec(ssh_client, cmd_stress)

def get_num_cores(ssh_client):
    num_cores_cmd = 'nproc --all'
    _, stdout, _ = ssh_client.exec_command(num_cores_cmd)
    return int(stdout.read())

# Main function for direct testing of above methods
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("public_vm_ip")
    parser.add_argument("container_id")
    parser.add_argument("--cpu_quota", type=int, default=0, help="Set CPU quota to CPU_QUOTA")
    parser.add_argument("--cpu_period", type=int, default=0, help="Set CPU period to CPU_PERIOD")
    parser.add_argument("--rst_cpu_quota", action="store_true", help="Reset any cpu shares imposed by --cpu_quota")
    parser.add_argument("--set_blkio", type=int, default=0, help="Set blkio to SET_BLKIO")
    parser.add_argument("--rst_blkio", action="store_true", help="Reset any blkio restrictions set by --set_blkio")
    parser.add_argument("--set_network", type=int, default=0, help="Set the network bandwidth to a specific value")
    parser.add_argument("--rst_network", action="store_true", help='Reset Network bandwidth to no throttling'
    )
    args = parser.parse_args()
    ssh_client = get_client(args.public_vm_ip)
    container_id = args.container_id
    cpu_period = args.cpu_period
    cpu_quota = args.cpu_quota
    set_blkio = args.set_blkio

    if args.cpu_quota and args.cpu_period and cpu_quota >= 0 and cpu_quota <= cpu_period:
        set_cpu_quota(ssh_client, container_id, cpu_period, cpu_quota)
    if args.rst_cpu_quota:
        reset_cpu_quota(ssh_client, container_id)
    if args.set_blkio and set_blkio >= 0 and set_blkio <= 70000000:
        change_container_blkio(ssh_client, container_id, set_blkio)
    if args.rst_blkio:
        change_container_blkio(ssh_client, container_id, 0)
    if args.set_network:
        set_egress_network_bandwidth(ssh_client, container_id, args.set_network)
    if args.rst_network:
        reset_egress_network_bandwidth(ssh_client, container_id)
