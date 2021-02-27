#! /usr/bin/python -u

import errno
import json
import netaddr
import netifaces
import os
import re
import subprocess
import sys

import bgp_common
import click
from click_default_group import DefaultGroup
from natsort import natsorted
from sonic_py_common import device_info, multi_asic
from swsssdk import ConfigDBConnector
from swsssdk import SonicV2Connector
from tabulate import tabulate
import mlnx
import utilities_common.cli as clicommon
import utilities_common.multi_asic as multi_asic_util


SONIC_CFGGEN_PATH = '/usr/local/bin/sonic-cfggen'

VLAN_SUB_INTERFACE_SEPARATOR = '.'

config_db = None

try:
    # noinspection PyPep8Naming
    import ConfigParser as configparser
except ImportError:
    # noinspection PyUnresolvedReferences
    import configparser


# This is from the aliases example:
# https://github.com/pallets/click/blob/57c6f09611fc47ca80db0bd010f05998b3c0aa95/examples/aliases/aliases.py
class Config(object):
    """Object to hold CLI config"""

    def __init__(self):
        self.path = os.getcwd()
        self.aliases = {}

    def read_config(self, filename):
        parser = configparser.RawConfigParser()
        parser.read([filename])
        try:
            self.aliases.update(parser.items('aliases'))
        except configparser.NoSectionError:
            pass

class InterfaceAliasConverter(object):
    """Class which handles conversion between interface name and alias"""

    def __init__(self):
        self.alias_max_length = 0
        self.port_dict = multi_asic.get_port_table()

        if not self.port_dict:
            click.echo(message="Warning: failed to retrieve PORT table from ConfigDB!", err=True)
            self.port_dict = {}

        for port_name in self.port_dict.keys():
            try:
                if self.alias_max_length < len(
                        self.port_dict[port_name]['alias']):
                   self.alias_max_length = len(
                        self.port_dict[port_name]['alias'])
            except KeyError:
                break

    def name_to_alias(self, interface_name):
        """Return vendor interface alias if SONiC
           interface name is given as argument
        """
        vlan_id = ''
        sub_intf_sep_idx = -1
        if interface_name is not None:
            sub_intf_sep_idx = interface_name.find(VLAN_SUB_INTERFACE_SEPARATOR)
            if sub_intf_sep_idx != -1:
                vlan_id = interface_name[sub_intf_sep_idx + 1:]
                # interface_name holds the parent port name
                interface_name = interface_name[:sub_intf_sep_idx]

            for port_name in self.port_dict.keys():
                if interface_name == port_name:
                    return self.port_dict[port_name]['alias'] if sub_intf_sep_idx == -1 \
                            else self.port_dict[port_name]['alias'] + VLAN_SUB_INTERFACE_SEPARATOR + vlan_id

        # interface_name not in port_dict. Just return interface_name
        return interface_name if sub_intf_sep_idx == -1 else interface_name + VLAN_SUB_INTERFACE_SEPARATOR + vlan_id

    def alias_to_name(self, interface_alias):
        """Return SONiC interface name if vendor
           port alias is given as argument
        """
        vlan_id = ''
        sub_intf_sep_idx = -1
        if interface_alias is not None:
            sub_intf_sep_idx = interface_alias.find(VLAN_SUB_INTERFACE_SEPARATOR)
            if sub_intf_sep_idx != -1:
                vlan_id = interface_alias[sub_intf_sep_idx + 1:]
                # interface_alias holds the parent port alias
                interface_alias = interface_alias[:sub_intf_sep_idx]

            for port_name in self.port_dict.keys():
                if interface_alias == self.port_dict[port_name]['alias']:
                    return port_name if sub_intf_sep_idx == -1 else port_name + VLAN_SUB_INTERFACE_SEPARATOR + vlan_id

        # interface_alias not in port_dict. Just return interface_alias
        return interface_alias if sub_intf_sep_idx == -1 else interface_alias + VLAN_SUB_INTERFACE_SEPARATOR + vlan_id


# Global Config object
_config = None


# This aliased group has been modified from click examples to inherit from DefaultGroup instead of click.Group.
# DefaultGroup is a superclass of click.Group which calls a default subcommand instead of showing
# a help message if no subcommand is passed
class AliasedGroup(DefaultGroup):
    """This subclass of a DefaultGroup supports looking up aliases in a config
    file and with a bit of magic.
    """

    def get_command(self, ctx, cmd_name):
        global _config

        # If we haven't instantiated our global config, do it now and load current config
        if _config is None:
            _config = Config()

            # Load our config file
            cfg_file = os.path.join(os.path.dirname(__file__), 'aliases.ini')
            _config.read_config(cfg_file)

        # Try to get builtin commands as normal
        rv = click.Group.get_command(self, ctx, cmd_name)
        if rv is not None:
            return rv

        # No builtin found. Look up an explicit command alias in the config
        if cmd_name in _config.aliases:
            actual_cmd = _config.aliases[cmd_name]
            return click.Group.get_command(self, ctx, actual_cmd)

        # Alternative option: if we did not find an explicit alias we
        # allow automatic abbreviation of the command.  "status" for
        # instance will match "st".  We only allow that however if
        # there is only one command.
        matches = [x for x in self.list_commands(ctx)
                   if x.lower().startswith(cmd_name.lower())]
        if not matches:
            # No command name matched. Issue Default command.
            ctx.arg0 = cmd_name
            cmd_name = self.default_cmd_name
            return DefaultGroup.get_command(self, ctx, cmd_name)
        elif len(matches) == 1:
            return DefaultGroup.get_command(self, ctx, matches[0])
        ctx.fail('Too many matches: %s' % ', '.join(sorted(matches)))


# To be enhanced. Routing-stack information should be collected from a global
# location (configdb?), so that we prevent the continous execution of this
# bash oneliner. To be revisited once routing-stack info is tracked somewhere.
def get_routing_stack():
    command = "sudo docker ps | grep bgp | awk '{print$2}' | cut -d'-' -f3 | cut -d':' -f1 | head -n 1"

    try:
        proc = subprocess.Popen(command,
                                stdout=subprocess.PIPE,
                                shell=True,
                                stderr=subprocess.STDOUT)
        stdout = proc.communicate()[0]
        proc.wait()
        result = stdout.rstrip('\n')

    except OSError, e:
        raise OSError("Cannot detect routing-stack")

    return (result)


# Global Routing-Stack variable
routing_stack = get_routing_stack()


def run_command(command, display_cmd=False, return_cmd=False):
    if display_cmd:
        click.echo(click.style("Command: ", fg='cyan') + click.style(command, fg='green'))

    # No conversion needed for intfutil commands as it already displays
    # both SONiC interface name and alias name for all interfaces.
    if get_interface_mode() == "alias" and not command.startswith("intfutil"):
        run_command_in_alias_mode(command)
        raise sys.exit(0)

    proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE)

    while True:
        if return_cmd:
            output = proc.communicate()[0].decode("utf-8")
            return output
        output = proc.stdout.readline()
        if output == "" and proc.poll() is not None:
            break
        if output:
            click.echo(output.rstrip('\n'))

    rc = proc.poll()
    if rc != 0:
        sys.exit(rc)


def get_interface_mode():
    mode = os.getenv('SONIC_CLI_IFACE_MODE')
    if mode is None:
        mode = "default"
    return mode


def is_ip_prefix_in_key(key):
    '''
    Function to check if IP address is present in the key. If it
    is present, then the key would be a tuple or else, it shall be
    be string
    '''
    return (isinstance(key, tuple))


# Global class instance for SONiC interface name to alias conversion
iface_alias_converter = InterfaceAliasConverter()


def print_output_in_alias_mode(output, index):
    """Convert and print all instances of SONiC interface
       name to vendor-sepecific interface aliases.
    """

    alias_name = ""
    interface_name = ""

    # Adjust tabulation width to length of alias name
    if output.startswith("---"):
        word = output.split()
        dword = word[index]
        underline = dword.rjust(iface_alias_converter.alias_max_length,
                                '-')
        word[index] = underline
        output = '  ' .join(word)

    # Replace SONiC interface name with vendor alias
    word = output.split()
    if word:
        interface_name = word[index]
        interface_name = interface_name.replace(':', '')
    for port_name in natsorted(iface_alias_converter.port_dict.keys()):
            if interface_name == port_name:
                alias_name = iface_alias_converter.port_dict[port_name]['alias']
    if alias_name:
        if len(alias_name) < iface_alias_converter.alias_max_length:
            alias_name = alias_name.rjust(
                                iface_alias_converter.alias_max_length)
        output = output.replace(interface_name, alias_name, 1)

    click.echo(output.rstrip('\n'))


def run_command_in_alias_mode(command):
    """Run command and replace all instances of SONiC interface names
       in output with vendor-sepecific interface aliases.
    """

    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE)

    while True:
        output = process.stdout.readline()
        if output == '' and process.poll() is not None:
            break

        if output:
            index = 1
            raw_output = output
            output = output.lstrip()

            if command.startswith("portstat"):
                """Show interface counters"""
                index = 0
                if output.startswith("IFACE"):
                    output = output.replace("IFACE", "IFACE".rjust(
                               iface_alias_converter.alias_max_length))
                print_output_in_alias_mode(output, index)

            elif command.startswith("intfstat"):
                """Show RIF counters"""
                index = 0
                if output.startswith("IFACE"):
                    output = output.replace("IFACE", "IFACE".rjust(
                               iface_alias_converter.alias_max_length))
                print_output_in_alias_mode(output, index)

            elif command == "pfcstat":
                """Show pfc counters"""
                index = 0
                if output.startswith("Port Tx"):
                    output = output.replace("Port Tx", "Port Tx".rjust(
                                iface_alias_converter.alias_max_length))

                elif output.startswith("Port Rx"):
                    output = output.replace("Port Rx", "Port Rx".rjust(
                                iface_alias_converter.alias_max_length))
                print_output_in_alias_mode(output, index)

            elif (command.startswith("sudo sfputil show eeprom")):
                """show interface transceiver eeprom"""
                index = 0
                print_output_in_alias_mode(raw_output, index)

            elif (command.startswith("sudo sfputil show")):
                """show interface transceiver lpmode,
                   presence
                """
                index = 0
                if output.startswith("Port"):
                    output = output.replace("Port", "Port".rjust(
                               iface_alias_converter.alias_max_length))
                print_output_in_alias_mode(output, index)

            elif command == "sudo lldpshow":
                """show lldp table"""
                index = 0
                if output.startswith("LocalPort"):
                    output = output.replace("LocalPort", "LocalPort".rjust(
                               iface_alias_converter.alias_max_length))
                print_output_in_alias_mode(output, index)

            elif command.startswith("queuestat"):
                """show queue counters"""
                index = 0
                if output.startswith("Port"):
                    output = output.replace("Port", "Port".rjust(
                               iface_alias_converter.alias_max_length))
                print_output_in_alias_mode(output, index)

            elif command == "fdbshow":
                """show mac"""
                index = 3
                if output.startswith("No."):
                    output = "  " + output
                    output = re.sub(
                                'Type', '      Type', output)
                elif output[0].isdigit():
                    output = "    " + output
                print_output_in_alias_mode(output, index)

            elif command.startswith("nbrshow"):
                """show arp"""
                index = 2
                if "Vlan" in output:
                    output = output.replace('Vlan', '  Vlan')
                print_output_in_alias_mode(output, index)

            elif command.startswith("sudo teamshow"):
                """
                sudo teamshow
                Search for port names either at the start of a line or preceded immediately by
                whitespace and followed immediately by either the end of a line or whitespace
                OR followed immediately by '(D)', '(S)', '(D*)' or '(S*)'
                """
                converted_output = raw_output
                for port_name in iface_alias_converter.port_dict.keys():
                    converted_output = re.sub(r"(^|\s){}(\([DS]\*{{0,1}}\)(?:$|\s))".format(port_name),
                            r"\1{}\2".format(iface_alias_converter.name_to_alias(port_name)),
                            converted_output)
                click.echo(converted_output.rstrip('\n'))

            else:
                """
                Default command conversion
                Search for port names either at the start of a line or preceded immediately by
                whitespace and followed immediately by either the end of a line or whitespace
                or a comma followed by whitespace
                """
                converted_output = raw_output
                for port_name in iface_alias_converter.port_dict.keys():
                    converted_output = re.sub(r"(^|\s){}($|,{{0,1}}\s)".format(port_name),
                            r"\1{}\2".format(iface_alias_converter.name_to_alias(port_name)),
                            converted_output)
                click.echo(converted_output.rstrip('\n'))

    rc = process.poll()
    if rc != 0:
        sys.exit(rc)

def connect_config_db():
    """
    Connects to config_db
    """
    config_db = ConfigDBConnector()
    config_db.connect()
    return config_db


CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help', '-?'])

#
# 'cli' group (root group)
#

# This is our entrypoint - the main "show" command
# TODO: Consider changing function name to 'show' for better understandability
@click.group(cls=AliasedGroup, context_settings=CONTEXT_SETTINGS)
def cli():
    """SONiC command line - 'show' command"""
    global config_db

    config_db = ConfigDBConnector()
    config_db.connect()

#
# 'vrf' command ("show vrf")
#

def get_interface_bind_to_vrf(config_db, vrf_name):
    """Get interfaces belong to vrf
    """
    tables = ['INTERFACE', 'PORTCHANNEL_INTERFACE', 'VLAN_INTERFACE', 'LOOPBACK_INTERFACE']
    data = []
    for table_name in tables:
        interface_dict = config_db.get_table(table_name)
        if interface_dict:
            for interface in interface_dict.keys():
                if interface_dict[interface].has_key('vrf_name') and vrf_name == interface_dict[interface]['vrf_name']:
                    data.append(interface)
    return data

@cli.command()
@click.argument('vrf_name', required=False)
def vrf(vrf_name):
    """Show vrf config"""
    config_db = ConfigDBConnector()
    config_db.connect()
    header = ['VRF', 'Interfaces']
    body = []
    vrf_dict = config_db.get_table('VRF')
    if vrf_dict:
        vrfs = []
        if vrf_name is None:
            vrfs = vrf_dict.keys()
        elif vrf_name in vrf_dict.keys():
            vrfs = [vrf_name]
        for vrf in vrfs:
            intfs = get_interface_bind_to_vrf(config_db, vrf)
            if len(intfs) == 0:
                body.append([vrf, ""])
            else:
                body.append([vrf, intfs[0]])
                for intf in intfs[1:]:
                    body.append(["", intf])
    click.echo(tabulate(body, header))

#
# 'arp' command ("show arp")
#

@cli.command()
@click.argument('ipaddress', required=False)
@click.option('-if', '--iface')
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def arp(ipaddress, iface, verbose):
    """Show IP ARP table"""
    cmd = "nbrshow -4"

    if ipaddress is not None:
        cmd += " -ip {}".format(ipaddress)

    if iface is not None:
        if get_interface_mode() == "alias":
            if not ((iface.startswith("PortChannel")) or
                    (iface.startswith("eth"))):
                iface = iface_alias_converter.alias_to_name(iface)

        cmd += " -if {}".format(iface)

    run_command(cmd, display_cmd=verbose)

#
# 'ndp' command ("show ndp")
#

@cli.command()
@click.argument('ip6address', required=False)
@click.option('-if', '--iface')
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def ndp(ip6address, iface, verbose):
    """Show IPv6 Neighbour table"""
    cmd = "nbrshow -6"

    if ip6address is not None:
        cmd += " -ip {}".format(ip6address)

    if iface is not None:
        cmd += " -if {}".format(iface)

    run_command(cmd, display_cmd=verbose)

def is_mgmt_vrf_enabled(ctx):
    """Check if management VRF is enabled"""
    if ctx.invoked_subcommand is None:
        cmd = 'sonic-cfggen -d --var-json "MGMT_VRF_CONFIG"'

        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try :
            mvrf_dict = json.loads(p.stdout.read())
        except ValueError:
            print("MGMT_VRF_CONFIG is not present.")
            return False

        # if the mgmtVrfEnabled attribute is configured, check the value
        # and return True accordingly.
        if 'mgmtVrfEnabled' in mvrf_dict['vrf_global']:
            if (mvrf_dict['vrf_global']['mgmtVrfEnabled'] == "true"):
                #ManagementVRF is enabled. Return True.
                return True

    return False

#
# 'mgmt-vrf' group ("show mgmt-vrf ...")
#

@cli.group('mgmt-vrf', invoke_without_command=True)
@click.argument('routes', required=False)
@click.pass_context
def mgmt_vrf(ctx,routes):
    """Show management VRF attributes"""

    if is_mgmt_vrf_enabled(ctx) is False:
        click.echo("\nManagementVRF : Disabled")
        return
    else:
        if routes is None:
            click.echo("\nManagementVRF : Enabled")
            click.echo("\nManagement VRF interfaces in Linux:")
            cmd = "ip -d link show mgmt"
            run_command(cmd)
            cmd = "ip link show vrf mgmt"
            run_command(cmd)
        else:
            click.echo("\nRoutes in Management VRF Routing Table:")
            cmd = "ip route show table 5000"
            run_command(cmd)

#
# 'management_interface' group ("show management_interface ...")
#

@cli.group(name='management_interface', cls=AliasedGroup, default_if_no_args=False)
def management_interface():
    """Show management interface parameters"""
    pass

# 'address' subcommand ("show management_interface address")
@management_interface.command()
def address ():
    """Show IP address configured for management interface"""

    config_db = ConfigDBConnector()
    config_db.connect()
    header = ['IFNAME', 'IP Address', 'PrefixLen',]
    body = []

    # Fetching data from config_db for MGMT_INTERFACE
    mgmt_ip_data = config_db.get_table('MGMT_INTERFACE')
    for key in natsorted(mgmt_ip_data.keys()):
        click.echo("Management IP address = {0}".format(key[1]))
        click.echo("Management Network Default Gateway = {0}".format(mgmt_ip_data[key]['gwaddr']))

#
# 'snmpagentaddress' group ("show snmpagentaddress ...")
#

@cli.group('snmpagentaddress', invoke_without_command=True)
@click.pass_context
def snmpagentaddress (ctx):
    """Show SNMP agent listening IP address configuration"""
    config_db = ConfigDBConnector()
    config_db.connect()
    agenttable = config_db.get_table('SNMP_AGENT_ADDRESS_CONFIG')

    header = ['ListenIP', 'ListenPort', 'ListenVrf']
    body = []
    for agent in agenttable.keys():
        body.append([agent[0], agent[1], agent[2]])
    click.echo(tabulate(body, header))

#
# 'snmptrap' group ("show snmptrap ...")
#

@cli.group('snmptrap', invoke_without_command=True)
@click.pass_context
def snmptrap (ctx):
    """Show SNMP agent Trap server configuration"""
    config_db = ConfigDBConnector()
    config_db.connect()
    traptable = config_db.get_table('SNMP_TRAP_CONFIG')

    header = ['Version', 'TrapReceiverIP', 'Port', 'VRF', 'Community']
    body = []
    for row in traptable.keys():
        if row == "v1TrapDest":
            ver=1
        elif row == "v2TrapDest":
            ver=2
        else:
            ver=3
        body.append([ver, traptable[row]['DestIp'], traptable[row]['DestPort'], traptable[row]['vrf'], traptable[row]['Community']])
    click.echo(tabulate(body, header))


#
# 'interfaces' group ("show interfaces ...")
#

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def interfaces():
    """Show details of the network interfaces"""
    pass

# 'alias' subcommand ("show interfaces alias")
@interfaces.command()
@click.argument('interfacename', required=False)
def alias(interfacename):
    """Show Interface Name/Alias Mapping"""

    cmd = 'sonic-cfggen -d --var-json "PORT"'
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)

    port_dict = json.loads(p.stdout.read())

    header = ['Name', 'Alias']
    body = []

    if interfacename is not None:
        if get_interface_mode() == "alias":
            interfacename = iface_alias_converter.alias_to_name(interfacename)

        # If we're given an interface name, output name and alias for that interface only
        if interfacename in port_dict:
            if 'alias' in port_dict[interfacename]:
                body.append([interfacename, port_dict[interfacename]['alias']])
            else:
                body.append([interfacename, interfacename])
        else:
            click.echo("Invalid interface name, '{0}'".format(interfacename))
            return
    else:
        # Output name and alias for all interfaces
        for port_name in natsorted(port_dict.keys()):
            if 'alias' in port_dict[port_name]:
                body.append([port_name, port_dict[port_name]['alias']])
            else:
                body.append([port_name, port_name])

    click.echo(tabulate(body, header))

#
# 'neighbor' group ###
#
@interfaces.group(cls=AliasedGroup, default_if_no_args=False)
def neighbor():
    """Show neighbor related information"""
    pass

# 'expected' subcommand ("show interface neighbor expected")
@neighbor.command()
@click.argument('interfacename', required=False)
def expected(interfacename):
    """Show expected neighbor information by interfaces"""
    neighbor_cmd = 'sonic-cfggen -d --var-json "DEVICE_NEIGHBOR"'
    p1 = subprocess.Popen(neighbor_cmd, shell=True, stdout=subprocess.PIPE)
    try :
        neighbor_dict = json.loads(p1.stdout.read())
    except ValueError:
        print("DEVICE_NEIGHBOR information is not present.")
        return

    neighbor_metadata_cmd = 'sonic-cfggen -d --var-json "DEVICE_NEIGHBOR_METADATA"'
    p2 = subprocess.Popen(neighbor_metadata_cmd, shell=True, stdout=subprocess.PIPE)
    try :
        neighbor_metadata_dict = json.loads(p2.stdout.read())
    except ValueError:
        print("DEVICE_NEIGHBOR_METADATA information is not present.")
        return

    for port in natsorted(neighbor_dict.keys()):
        temp_port = port
        if get_interface_mode() == "alias":
            port = iface_alias_converter.name_to_alias(port)
            neighbor_dict[port] = neighbor_dict.pop(temp_port)

    header = ['LocalPort', 'Neighbor', 'NeighborPort', 'NeighborLoopback', 'NeighborMgmt', 'NeighborType']
    body = []
    if interfacename:
        try:
            device = neighbor_dict[interfacename]['name']
            body.append([interfacename,
                         device,
                         neighbor_dict[interfacename]['port'],
                         neighbor_metadata_dict[device]['lo_addr'],
                         neighbor_metadata_dict[device]['mgmt_addr'],
                         neighbor_metadata_dict[device]['type']])
        except KeyError:
            click.echo("No neighbor information available for interface {}".format(interfacename))
            return
    else:
        for port in natsorted(neighbor_dict.keys()):
            try:
                device = neighbor_dict[port]['name']
                body.append([port,
                             device,
                             neighbor_dict[port]['port'],
                             neighbor_metadata_dict[device]['lo_addr'],
                             neighbor_metadata_dict[device]['mgmt_addr'],
                             neighbor_metadata_dict[device]['type']])
            except KeyError:
                pass

    click.echo(tabulate(body, header))

@interfaces.group(cls=AliasedGroup, default_if_no_args=False)
def transceiver():
    """Show SFP Transceiver information"""
    pass


@transceiver.command()
@click.argument('interfacename', required=False)
@click.option('-d', '--dom', 'dump_dom', is_flag=True, help="Also display Digital Optical Monitoring (DOM) data")
@click.option('--namespace', '-n', 'namespace', default=None, show_default=True,
              type=click.Choice(multi_asic_util.multi_asic_ns_choices()), help='Namespace name or all')
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def eeprom(interfacename, dump_dom, namespace, verbose):
    """Show interface transceiver EEPROM information"""

    cmd = "sfpshow eeprom"

    if dump_dom:
        cmd += " --dom"

    if interfacename is not None:
        if get_interface_mode() == "alias":
            interfacename = iface_alias_converter.alias_to_name(interfacename)

        cmd += " -p {}".format(interfacename)

    if namespace is not None:
        cmd += " -n {}".format(namespace)

    run_command(cmd, display_cmd=verbose)

@transceiver.command()
@click.argument('interfacename', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def lpmode(interfacename, verbose):
    """Show interface transceiver low-power mode status"""

    cmd = "sudo sfputil show lpmode"

    if interfacename is not None:
        if get_interface_mode() == "alias":
            interfacename = iface_alias_converter.alias_to_name(interfacename)

        cmd += " -p {}".format(interfacename)

    run_command(cmd, display_cmd=verbose)

@transceiver.command()
@click.argument('interfacename', required=False)
@click.option('--namespace', '-n', 'namespace', default=None, show_default=True,
              type=click.Choice(multi_asic_util.multi_asic_ns_choices()), help='Namespace name or all')
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def presence(interfacename, namespace, verbose):
    """Show interface transceiver presence"""

    cmd = "sfpshow presence"

    if interfacename is not None:
        if get_interface_mode() == "alias":
            interfacename = iface_alias_converter.alias_to_name(interfacename)

        cmd += " -p {}".format(interfacename)

    if namespace is not None:
        cmd += " -n {}".format(namespace)

    run_command(cmd, display_cmd=verbose)


@interfaces.command()
@click.argument('interfacename', required=False)
@multi_asic_util.multi_asic_click_options
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def description(interfacename, namespace, display, verbose):
    """Show interface status, protocol and description"""

    cmd = "intfutil -c description"

    if interfacename is not None:
        if get_interface_mode() == "alias":
            interfacename = iface_alias_converter.alias_to_name(interfacename)

        cmd += " -i {}".format(interfacename)
    else:
        cmd += " -d {}".format(display)
    
    if namespace is not None:
        cmd += " -n {}".format(namespace)

    run_command(cmd, display_cmd=verbose)


@interfaces.command()
@click.argument('interfacename', required=False)
@multi_asic_util.multi_asic_click_options
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def status(interfacename, namespace, display, verbose):
    """Show Interface status information"""

    cmd = "intfutil -c status"

    if interfacename is not None:
        if get_interface_mode() == "alias":
            interfacename = iface_alias_converter.alias_to_name(interfacename)

        cmd += " -i {}".format(interfacename)
    else:
            cmd += " -d {}".format(display)

    if namespace is not None:
        cmd += " -n {}".format(namespace)
    run_command(cmd, display_cmd=verbose)


# 'counters' subcommand ("show interfaces counters")
@interfaces.group(invoke_without_command=True)
@click.option('-a', '--printall', is_flag=True)
@click.option('-p', '--period')
@multi_asic_util.multi_asic_click_options
@click.option('--verbose', is_flag=True, help="Enable verbose output")
@click.pass_context
def counters(ctx, verbose, period, printall, namespace, display):
    """Show interface counters"""

    if ctx.invoked_subcommand is None:
        cmd = "portstat"

        if printall:
            cmd += " -a"
        if period is not None:
            cmd += " -p {}".format(period)

        cmd += " -s {}".format(display)
        if namespace is not None:
            cmd += " -n {}".format(namespace)

        run_command(cmd, display_cmd=verbose)

# 'counters' subcommand ("show interfaces counters rif")
@counters.command()
@click.argument('interface', metavar='<interface_name>', required=False, type=str)
@click.option('-p', '--period')
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def rif(interface, period, verbose):
    """Show interface counters"""

    cmd = "intfstat"
    if period is not None:
        cmd += " -p {}".format(period)
    if interface is not None:
        cmd += " -i {}".format(interface)

    run_command(cmd, display_cmd=verbose)

# 'portchannel' subcommand ("show interfaces portchannel")
@interfaces.command()
@multi_asic_util.multi_asic_click_options
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def portchannel(namespace,  display, verbose):
    """Show PortChannel information"""
    cmd = "sudo teamshow -d {}".format(display)
    if namespace is not None:
        cmd += "  -n {}".format(namespace)
    
    run_command(cmd, display_cmd=verbose)

# 'txerror' subcommand ("show interfaces txerror")
@interfaces.command()
@click.argument('interfacename', required=False)
def txerror(interfacename):
    """Show Interface TxError Information """
    
    db = SonicV2Connector(host='127.0.0.1')
    db.connect(db.STATE_DB, False)   # Make one attempt only
    
    
    TABLE_NAME_SEPARATOR = '|'
    prefix_statedb = "TX_ERR_STATE|"
    
    _hash = '{}{}'.format(prefix_statedb, '*')
    txerr_keys = db.keys(db.STATE_DB, _hash)
    
    table = []
    
    header = ['Port', 'Status', 'sai_port_id', 'Last Updated On', 'Tx Error Threshold']
    
    if txerr_keys is None:
        txerr_keys = dict()
        
    for k in txerr_keys:
        k = k.replace(prefix_statedb, "") 
        r = []
        r.append(k)
        
        entry = db.get_all(db.STATE_DB, prefix_statedb + k)
        
        if 'tx_error_stats' not in entry:
            r.append("UnKnown")
        else:
            r.append(entry['tx_error_stats'])
        
        if 'tx_error_portid' not in entry:
            r.append("")
        else:
            r.append(entry['tx_error_portid'])
        
        if 'tx_error_verified_latest_by' not in entry:
            r.append("")
        else:
            r.append(entry['tx_error_verified_latest_by'])
	
	if 'tx_error_threshold' not in entry:
            r.append("")
        else:
            r.append(entry['tx_error_threshold'])
        
        table.append(r)
    
    click.echo(tabulate(table, header))
        
    
    
#
# 'subinterfaces' group ("show subinterfaces ...")
#

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def subinterfaces():
    """Show details of the sub port interfaces"""
    pass

# 'subinterfaces' subcommand ("show subinterfaces status")
@subinterfaces.command()
@click.argument('subinterfacename', type=str, required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def status(subinterfacename, verbose):
    """Show sub port interface status information"""
    cmd = "intfutil -c status"

    if subinterfacename is not None:
        sub_intf_sep_idx = subinterfacename.find(VLAN_SUB_INTERFACE_SEPARATOR)
        if sub_intf_sep_idx == -1:
            print("Invalid sub port interface name")
            return

        if get_interface_mode() == "alias":
            subinterfacename = iface_alias_converter.alias_to_name(subinterfacename)

        cmd +=  " -i {}".format(subinterfacename)
    else:
        cmd += " -i subport"
    run_command(cmd, display_cmd=verbose)

#
# 'pfc' group ("show pfc ...")
#

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def pfc():
    """Show details of the priority-flow-control (pfc) """
    pass

# 'counters' subcommand ("show interfaces pfccounters")
@pfc.command()
@multi_asic_util.multi_asic_click_options
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def counters(namespace, display, verbose):
    """Show pfc counters"""

    cmd = "pfcstat -s {}".format(display)
    if namespace is not None:
        cmd += " -n {}".format(namespace)

    run_command(cmd, display_cmd=verbose)

# 'pfcwd' subcommand ("show pfcwd...")
@cli.group(cls=AliasedGroup, default_if_no_args=False)
def pfcwd():
    """Show details of the pfc watchdog """
    pass

@pfcwd.command()
@multi_asic_util.multi_asic_click_options
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def config(namespace, display, verbose):
    """Show pfc watchdog config"""

    cmd = "pfcwd show config -d {}".format(display)
    if namespace is not None:
        cmd += " -n {}".format(namespace)

    run_command(cmd, display_cmd=verbose)

@pfcwd.command()
@multi_asic_util.multi_asic_click_options
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def stats(namespace, display, verbose):
    """Show pfc watchdog stats"""

    cmd = "pfcwd show stats -d {}".format(display)
    if namespace is not None:
        cmd += " -n {}".format(namespace)

    run_command(cmd, display_cmd=verbose)

# 'naming_mode' subcommand ("show interfaces naming_mode")
@interfaces.command('naming_mode')
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def naming_mode(verbose):
    """Show interface naming_mode status"""

    click.echo(get_interface_mode())


#
# 'watermark' group ("show watermark telemetry interval")
#

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def watermark():
    """Show details of watermark """
    pass

@watermark.group()
def telemetry():
    """Show watermark telemetry info"""
    pass

@telemetry.command('interval')
def show_tm_interval():
    """Show telemetry interval"""
    command = 'watermarkcfg --show-interval'
    run_command(command)


#
# 'queue' group ("show queue ...")
#

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def queue():
    """Show details of the queues """
    pass

# 'counters' subcommand ("show queue counters")
@queue.command()
@click.argument('interfacename', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def counters(interfacename, verbose):
    """Show queue counters"""

    cmd = "queuestat"

    if interfacename is not None:
        if get_interface_mode() == "alias":
            interfacename = iface_alias_converter.alias_to_name(interfacename)

    if interfacename is not None:
        cmd += " -p {}".format(interfacename)

    run_command(cmd, display_cmd=verbose)

#
# 'watermarks' subgroup ("show queue watermarks ...")
#

@queue.group()
def watermark():
    """Show user WM for queues"""
    pass

# 'unicast' subcommand ("show queue watermarks unicast")
@watermark.command('unicast')
def wm_q_uni():
    """Show user WM for unicast queues"""
    command = 'watermarkstat -t q_shared_uni'
    run_command(command)

# 'multicast' subcommand ("show queue watermarks multicast")
@watermark.command('multicast')
def wm_q_multi():
    """Show user WM for multicast queues"""
    command = 'watermarkstat -t q_shared_multi'
    run_command(command)

#
# 'persistent-watermarks' subgroup ("show queue persistent-watermarks ...")
#

@queue.group(name='persistent-watermark')
def persistent_watermark():
    """Show persistent WM for queues"""
    pass

# 'unicast' subcommand ("show queue persistent-watermarks unicast")
@persistent_watermark.command('unicast')
def pwm_q_uni():
    """Show persistent WM for unicast queues"""
    command = 'watermarkstat -p -t q_shared_uni'
    run_command(command)

# 'multicast' subcommand ("show queue persistent-watermarks multicast")
@persistent_watermark.command('multicast')
def pwm_q_multi():
    """Show persistent WM for multicast queues"""
    command = 'watermarkstat -p -t q_shared_multi'
    run_command(command)


#
# 'priority-group' group ("show priority-group ...")
#

@cli.group(name='priority-group', cls=AliasedGroup, default_if_no_args=False)
def priority_group():
    """Show details of the PGs """

@priority_group.group()
def watermark():
    """Show priority-group user WM"""
    pass

@watermark.command('headroom')
def wm_pg_headroom():
    """Show user headroom WM for pg"""
    command = 'watermarkstat -t pg_headroom'
    run_command(command)

@watermark.command('shared')
def wm_pg_shared():
    """Show user shared WM for pg"""
    command = 'watermarkstat -t pg_shared'
    run_command(command)

@priority_group.group(name='persistent-watermark')
def persistent_watermark():
    """Show priority-group persistent WM"""
    pass

@persistent_watermark.command('headroom')
def pwm_pg_headroom():
    """Show persistent headroom WM for pg"""
    command = 'watermarkstat -p -t pg_headroom'
    run_command(command)

@persistent_watermark.command('shared')
def pwm_pg_shared():
    """Show persistent shared WM for pg"""
    command = 'watermarkstat -p -t pg_shared'
    run_command(command)


#
# 'buffer_pool' group ("show buffer_pool ...")
#

@cli.group(name='buffer_pool', cls=AliasedGroup, default_if_no_args=False)
def buffer_pool():
    """Show details of the buffer pools"""

@buffer_pool.command('watermark')
def wm_buffer_pool():
    """Show user WM for buffer pools"""
    command = 'watermarkstat -t buffer_pool'
    run_command(command)

@buffer_pool.command('persistent-watermark')
def pwm_buffer_pool():
    """Show persistent WM for buffer pools"""
    command = 'watermarkstat -p -t buffer_pool'
    run_command(command)


#
# 'mac' command ("show mac ...")
#

@cli.command()
@click.option('-v', '--vlan')
@click.option('-p', '--port')
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def mac(vlan, port, verbose):
    """Show MAC (FDB) entries"""

    cmd = "fdbshow"

    if vlan is not None:
        cmd += " -v {}".format(vlan)

    if port is not None:
        cmd += " -p {}".format(port)

    run_command(cmd, display_cmd=verbose)

#
# 'show route-map' command ("show route-map")
#

@cli.command('route-map')
@click.argument('route_map_name', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def route_map(route_map_name, verbose):
    """show route-map"""
    cmd = 'sudo vtysh -c "show route-map'
    if route_map_name is not None:
        cmd += ' {}'.format(route_map_name)
    cmd += '"'
    run_command(cmd, display_cmd=verbose)

#
# 'ip' group ("show ip ...")
#

# This group houses IP (i.e., IPv4) commands and subgroups
@cli.group(cls=AliasedGroup, default_if_no_args=False)
def ip():
    """Show IP (IPv4) commands"""
    pass


#
# get_if_admin_state
#
# Given an interface name, return its admin state reported by the kernel.
#
def get_if_admin_state(iface):
    admin_file = "/sys/class/net/{0}/flags"

    try:
        state_file = open(admin_file.format(iface), "r")
    except IOError as e:
        print "Error: unable to open file: %s" % str(e)
        return "error"

    content = state_file.readline().rstrip()
    flags = int(content, 16)

    if flags & 0x1:
        return "up"
    else:
        return "down"


#
# get_if_oper_state
#
# Given an interface name, return its oper state reported by the kernel.
#
def get_if_oper_state(iface):
    oper_file = "/sys/class/net/{0}/carrier"

    try:
        state_file = open(oper_file.format(iface), "r")
    except IOError as e:
        print "Error: unable to open file: %s" % str(e)
        return "error"

    oper_state = state_file.readline().rstrip()
    if oper_state == "1":
        return "up"
    else:
        return "down"


#
# get_if_master
#
# Given an interface name, return its master reported by the kernel.
#
def get_if_master(iface):
    oper_file = "/sys/class/net/{0}/master"

    if os.path.exists(oper_file.format(iface)):
        real_path = os.path.realpath(oper_file.format(iface))
        return os.path.basename(real_path)
    else:
        return ""


#
# 'show ip interfaces' command
#
# Display all interfaces with master, an IPv4 address, admin/oper states, their BGP neighbor name and peer ip.
# Addresses from all scopes are included. Interfaces with no addresses are
# excluded.
#
@ip.command()
def interfaces():
    """Show interfaces IPv4 address"""
    header = ['Interface', 'Master', 'IPv4 address/mask', 'Admin/Oper', 'BGP Neighbor', 'Neighbor IP']
    data = []
    bgp_peer = get_bgp_peer()

    interfaces = natsorted(netifaces.interfaces())

    for iface in interfaces:
        ipaddresses = netifaces.ifaddresses(iface)

        if netifaces.AF_INET in ipaddresses:
            ifaddresses = []
            for ipaddr in ipaddresses[netifaces.AF_INET]:
                neighbor_name = 'N/A'
                neighbor_ip = 'N/A'
                local_ip = str(ipaddr['addr'])
                netmask = netaddr.IPAddress(ipaddr['netmask']).netmask_bits()
                ifaddresses.append(["", local_ip + "/" + str(netmask)])
                try:
                    neighbor_name = bgp_peer[local_ip][0]
                    neighbor_ip = bgp_peer[local_ip][1]
                except:
                    pass

            if len(ifaddresses) > 0:
                admin = get_if_admin_state(iface)
                if admin == "up":
                    oper = get_if_oper_state(iface)
                else:
                    oper = "down"
                master = get_if_master(iface)
                if get_interface_mode() == "alias":
                    iface = iface_alias_converter.name_to_alias(iface)

                data.append([iface, master, ifaddresses[0][1], admin + "/" + oper, neighbor_name, neighbor_ip])

            for ifaddr in ifaddresses[1:]:
                data.append(["", "", ifaddr[1], ""])

    print tabulate(data, header, tablefmt="simple", stralign='left', missingval="")

# get bgp peering info
def get_bgp_peer():
    """
    collects local and bgp neighbor ip along with device name in below format
    {
     'local_addr1':['neighbor_device1_name', 'neighbor_device1_ip'],
     'local_addr2':['neighbor_device2_name', 'neighbor_device2_ip']
     }
    """
    config_db = ConfigDBConnector()
    config_db.connect()
    bgp_peer = {}
    bgp_neighbor_tables = ['BGP_NEIGHBOR', 'BGP_INTERNAL_NEIGHBOR']

    for table in bgp_neighbor_tables:
        data = config_db.get_table(table)
        for neighbor_ip in data.keys():
            local_addr = data[neighbor_ip]['local_addr']
            neighbor_name = data[neighbor_ip]['name']
            bgp_peer.setdefault(local_addr, [neighbor_name, neighbor_ip])

    return bgp_peer

#
# 'route' subcommand ("show ip route")
#

@ip.command()
@click.argument('args', metavar='[IPADDRESS] [vrf <vrf_name>] [...]', nargs=-1, required=False)
@click.option('--display', '-d', 'display', default=None, show_default=False, type=str, help='all|frontend')
@click.option('--namespace', '-n', 'namespace', default=None, type=str, show_default=False, help='Namespace name or all')
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def route(args, namespace, display, verbose):
    """Show IP (IPv4) routing table"""
    # Call common handler to handle the show ip route cmd
    bgp_common.show_routes(args, namespace, display, verbose, "ip")

#
# 'prefix-list' subcommand ("show ip prefix-list")
#

@ip.command('prefix-list')
@click.argument('prefix_list_name', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def prefix_list(prefix_list_name, verbose):
    """show ip prefix-list"""
    cmd = 'sudo vtysh -c "show ip prefix-list'
    if prefix_list_name is not None:
        cmd += ' {}'.format(prefix_list_name)
    cmd += '"'
    run_command(cmd, display_cmd=verbose)


# 'protocol' command
@ip.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def protocol(verbose):
    """Show IPv4 protocol information"""
    cmd = 'sudo vtysh -c "show ip protocol"'
    run_command(cmd, display_cmd=verbose)


#
# 'ipv6' group ("show ipv6 ...")
#

# This group houses IPv6-related commands and subgroups
@cli.group(cls=AliasedGroup, default_if_no_args=False)
def ipv6():
    """Show IPv6 commands"""
    pass

#
# 'prefix-list' subcommand ("show ipv6 prefix-list")
#

@ipv6.command('prefix-list')
@click.argument('prefix_list_name', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def prefix_list(prefix_list_name, verbose):
    """show ip prefix-list"""
    cmd = 'sudo vtysh -c "show ipv6 prefix-list'
    if prefix_list_name is not None:
        cmd += ' {}'.format(prefix_list_name)
    cmd += '"'
    run_command(cmd, display_cmd=verbose)



#
# 'show ipv6 interfaces' command
#
# Display all interfaces with master, an IPv6 address, admin/oper states, their BGP neighbor name and peer ip.
# Addresses from all scopes are included. Interfaces with no addresses are
# excluded.
#
@ipv6.command()
def interfaces():
    """Show interfaces IPv6 address"""
    header = ['Interface', 'Master', 'IPv6 address/mask', 'Admin/Oper', 'BGP Neighbor', 'Neighbor IP']
    data = []
    bgp_peer = get_bgp_peer()

    interfaces = natsorted(netifaces.interfaces())

    for iface in interfaces:
        ipaddresses = netifaces.ifaddresses(iface)

        if netifaces.AF_INET6 in ipaddresses:
            ifaddresses = []
            for ipaddr in ipaddresses[netifaces.AF_INET6]:
                neighbor_name = 'N/A'
                neighbor_ip = 'N/A'
                local_ip = str(ipaddr['addr'])
                netmask = ipaddr['netmask'].split('/', 1)[-1]
                ifaddresses.append(["", local_ip + "/" + str(netmask)])
                try:
                    neighbor_name = bgp_peer[local_ip][0]
                    neighbor_ip = bgp_peer[local_ip][1]
                except:
                    pass

            if len(ifaddresses) > 0:
                admin = get_if_admin_state(iface)
                if admin == "up":
                    oper = get_if_oper_state(iface)
                else:
                    oper = "down"
                master = get_if_master(iface)
                if get_interface_mode() == "alias":
                    iface = iface_alias_converter.name_to_alias(iface)
                data.append([iface, master, ifaddresses[0][1], admin + "/" + oper, neighbor_name, neighbor_ip])
            for ifaddr in ifaddresses[1:]:
                data.append(["", "", ifaddr[1], ""])

    print tabulate(data, header, tablefmt="simple", stralign='left', missingval="")


#
# 'route' subcommand ("show ipv6 route")
#

@ipv6.command()
@click.argument('args', metavar='[IPADDRESS] [vrf <vrf_name>] [...]', nargs=-1, required=False)
@click.option('--display', '-d', 'display', default=None, show_default=False, type=str, help='all|frontend')
@click.option('--namespace', '-n', 'namespace', default=None, type=str, show_default=False, help='Namespace name or all')
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def route(args, namespace, display, verbose):
    """Show IPv6 routing table"""
    # Call common handler to handle the show ipv6 route cmd
    bgp_common.show_routes(args, namespace, display, verbose, "ipv6")


# 'protocol' command
@ipv6.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def protocol(verbose):
    """Show IPv6 protocol information"""
    cmd = 'sudo vtysh -c "show ipv6 protocol"'
    run_command(cmd, display_cmd=verbose)


#
# Inserting BGP functionality into cli's show parse-chain.
# BGP commands are determined by the routing-stack being elected.
#
if routing_stack == "quagga":
    from .bgp_quagga_v4 import bgp
    ip.add_command(bgp)
    from .bgp_quagga_v6 import bgp
    ipv6.add_command(bgp)
elif routing_stack == "frr":
    from .bgp_frr_v4 import bgp
    ip.add_command(bgp)
    from .bgp_frr_v6 import bgp
    ipv6.add_command(bgp)

#
# 'lldp' group ("show lldp ...")
#

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def lldp():
    """LLDP (Link Layer Discovery Protocol) information"""
    pass

# Default 'lldp' command (called if no subcommands or their aliases were passed)
@lldp.command()
@click.argument('interfacename', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def neighbors(interfacename, verbose):
    """Show LLDP neighbors"""
    cmd = "sudo lldpshow -d"

    if interfacename is not None:
        if get_interface_mode() == "alias":
            interfacename = iface_alias_converter.alias_to_name(interfacename)

        cmd += " -p {}".format(interfacename)

    run_command(cmd, display_cmd=verbose)

# 'table' subcommand ("show lldp table")
@lldp.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def table(verbose):
    """Show LLDP neighbors in tabular format"""
    cmd = "sudo lldpshow"
    run_command(cmd, display_cmd=verbose)

#
# 'platform' group ("show platform ...")
#

def get_hw_info_dict():
    """
    This function is used to get the HW info helper function
    """
    hw_info_dict = {}

    version_info = device_info.get_sonic_version_info()

    hw_info_dict['platform'] = device_info.get_platform()
    hw_info_dict['hwsku'] = device_info.get_hwsku()
    hw_info_dict['asic_type'] = version_info['asic_type']
    hw_info_dict['asic_count'] = device_info.get_num_npus()

    return hw_info_dict

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def platform():
    """Show platform-specific hardware info"""
    pass

version_info = device_info.get_sonic_version_info()
if (version_info and version_info.get('asic_type') == 'mellanox'):
    platform.add_command(mlnx.mlnx)

# 'summary' subcommand ("show platform summary")
@platform.command()
@click.option('--json', is_flag=True, help="JSON output")
def summary(json):
    """Show hardware platform information"""

    hw_info_dict = get_hw_info_dict()
    if json:
        click.echo(clicommon.json_dump(hw_info_dict))
    else:
        click.echo("Platform: {}".format(hw_info_dict['platform']))
        click.echo("HwSKU: {}".format(hw_info_dict['hwsku']))
        click.echo("ASIC: {}".format(hw_info_dict['asic_type']))
        click.echo("ASIC Count: {}".format(hw_info_dict['asic_count']))

# 'syseeprom' subcommand ("show platform syseeprom")
@platform.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def syseeprom(verbose):
    """Show system EEPROM information"""
    cmd = "sudo decode-syseeprom -d"
    run_command(cmd, display_cmd=verbose)

# 'psustatus' subcommand ("show platform psustatus")
@platform.command()
@click.option('-i', '--index', default=-1, type=int, help="the index of PSU")
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def psustatus(index, verbose):
    """Show PSU status information"""
    cmd = "psushow -s"

    if index >= 0:
        cmd += " -i {}".format(index)

    run_command(cmd, display_cmd=verbose)

# 'ssdhealth' subcommand ("show platform ssdhealth [--verbose/--vendor]")
@platform.command()
@click.argument('device', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
@click.option('--vendor', is_flag=True, help="Enable vendor specific output")
def ssdhealth(device, verbose, vendor):
    """Show SSD Health information"""
    if not device:
        device = os.popen("lsblk -o NAME,TYPE -p | grep disk").readline().strip().split()[0]
    cmd = "ssdutil -d " + device
    options = " -v" if verbose else ""
    options += " -e" if vendor else ""
    run_command(cmd + options, display_cmd=verbose)

# 'fan' subcommand ("show platform fan")
@platform.command()
def fan():
    """Show fan status information"""
    cmd = 'fanshow'
    run_command(cmd)

# 'temperature' subcommand ("show platform temperature")
@platform.command()
def temperature():
    """Show device temperature information"""
    cmd = 'tempershow'
    run_command(cmd)

# 'firmware' subcommand ("show platform firmware")
@platform.command(
    context_settings=dict(
        ignore_unknown_options=True,
        allow_extra_args=True
    ),
    add_help_option=False
)
@click.argument('args', nargs=-1, type=click.UNPROCESSED)
def firmware(args):
    """Show firmware information"""
    cmd = "fwutil show {}".format(" ".join(args))

    try:
        subprocess.check_call(cmd, shell=True)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)

#
# 'logging' command ("show logging")
#

@cli.command()
@click.argument('process', required=False)
@click.option('-l', '--lines')
@click.option('-f', '--follow', is_flag=True)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def logging(process, lines, follow, verbose):
    """Show system log"""
    if follow:
        cmd = "sudo tail -F /var/log/syslog"
        run_command(cmd, display_cmd=verbose)
    else:
        if os.path.isfile("/var/log/syslog.1"):
            cmd = "sudo cat /var/log/syslog.1 /var/log/syslog"
        else:
            cmd = "sudo cat /var/log/syslog"

        if process is not None:
            cmd += " | grep '{}'".format(process)

        if lines is not None:
            cmd += " | tail -{}".format(lines)

        run_command(cmd, display_cmd=verbose)


#
# 'version' command ("show version")
#

@cli.command()
@click.option("--verbose", is_flag=True, help="Enable verbose output")
def version(verbose):
    """Show version information"""
    version_info = device_info.get_sonic_version_info()
    hw_info_dict = get_hw_info_dict()
    serial_number_cmd = "sudo decode-syseeprom -s"
    serial_number = subprocess.Popen(serial_number_cmd, shell=True, stdout=subprocess.PIPE)
    sys_uptime_cmd = "uptime"
    sys_uptime = subprocess.Popen(sys_uptime_cmd, shell=True, stdout=subprocess.PIPE)
    click.echo("\nSONiC Software Version: SONiC.{}".format(version_info['build_version']))
    click.echo("Distribution: Debian {}".format(version_info['debian_version']))
    click.echo("Kernel: {}".format(version_info['kernel_version']))
    click.echo("Build commit: {}".format(version_info['commit_id']))
    click.echo("Build date: {}".format(version_info['build_date']))
    click.echo("Built by: {}".format(version_info['built_by']))
    click.echo("\nPlatform: {}".format(hw_info_dict['platform']))
    click.echo("HwSKU: {}".format(hw_info_dict['hwsku']))
    click.echo("ASIC: {}".format(hw_info_dict['asic_type']))
    click.echo("Serial Number: {}".format(serial_number.stdout.read().strip()))
    click.echo("Uptime: {}".format(sys_uptime.stdout.read().strip()))
    click.echo("\nDocker images:")
    cmd = 'sudo docker images --format "table {{.Repository}}\\t{{.Tag}}\\t{{.ID}}\\t{{.Size}}"'
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE)
    click.echo(p.stdout.read())

#
# 'environment' command ("show environment")
#

@cli.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def environment(verbose):
    """Show environmentals (voltages, fans, temps)"""
    cmd = "sudo sensors"
    run_command(cmd, display_cmd=verbose)


#
# 'processes' group ("show processes ...")
#

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def processes():
    """Display process information"""
    pass

@processes.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def summary(verbose):
    """Show processes info"""
    # Run top batch mode to prevent unexpected newline after each newline
    cmd = "ps -eo pid,ppid,cmd,%mem,%cpu "
    run_command(cmd, display_cmd=verbose)


# 'cpu' subcommand ("show processes cpu")
@processes.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def cpu(verbose):
    """Show processes CPU info"""
    # Run top in batch mode to prevent unexpected newline after each newline
    cmd = "top -bn 1 -o %CPU"
    run_command(cmd, display_cmd=verbose)

# 'memory' subcommand
@processes.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def memory(verbose):
    """Show processes memory info"""
    # Run top batch mode to prevent unexpected newline after each newline
    cmd = "top -bn 1 -o %MEM"
    run_command(cmd, display_cmd=verbose)

#
# 'users' command ("show users")
#

@cli.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def users(verbose):
    """Show users"""
    cmd = "who"
    run_command(cmd, display_cmd=verbose)


#
# 'techsupport' command ("show techsupport")
#

@cli.command()
@click.option('--since', required=False, help="Collect logs and core files since given date")
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def techsupport(since, verbose):
    """Gather information for troubleshooting"""
    cmd = "sudo generate_dump -v"
    if since:
        cmd += " -s {}".format(since)
    run_command(cmd, display_cmd=verbose)


#
# 'runningconfiguration' group ("show runningconfiguration")
#

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def runningconfiguration():
    """Show current running configuration information"""
    pass


# 'all' subcommand ("show runningconfiguration all")
@runningconfiguration.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def all(verbose):
    """Show full running configuration"""
    cmd = "sonic-cfggen -d --print-data"
    run_command(cmd, display_cmd=verbose)


# 'acl' subcommand ("show runningconfiguration acl")
@runningconfiguration.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def acl(verbose):
    """Show acl running configuration"""
    cmd = "sonic-cfggen -d --var-json ACL_RULE"
    run_command(cmd, display_cmd=verbose)


# 'ports' subcommand ("show runningconfiguration ports <portname>")
@runningconfiguration.command()
@click.argument('portname', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def ports(portname, verbose):
    """Show ports running configuration"""
    cmd = "sonic-cfggen -d --var-json PORT"

    if portname is not None:
        cmd += " {0} {1}".format("--key", portname)

    run_command(cmd, display_cmd=verbose)


# 'bgp' subcommand ("show runningconfiguration bgp")
@runningconfiguration.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def bgp(verbose):
    """Show BGP running configuration"""
    cmd = 'sudo vtysh -c "show running-config"'
    run_command(cmd, display_cmd=verbose)


# 'interfaces' subcommand ("show runningconfiguration interfaces")
@runningconfiguration.command()
@click.argument('interfacename', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def interfaces(interfacename, verbose):
    """Show interfaces running configuration"""
    cmd = "sonic-cfggen -d --var-json INTERFACE"

    if interfacename is not None:
        cmd += " {0} {1}".format("--key", interfacename)

    run_command(cmd, display_cmd=verbose)


# 'snmp' subcommand ("show runningconfiguration snmp")
@runningconfiguration.command()
@click.argument('server', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def snmp(server, verbose):
    """Show SNMP information"""
    cmd = "sudo docker exec snmp cat /etc/snmp/snmpd.conf"

    if server is not None:
        cmd += " | grep -i agentAddress"

    run_command(cmd, display_cmd=verbose)


# 'ntp' subcommand ("show runningconfiguration ntp")
@runningconfiguration.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def ntp(verbose):
    """Show NTP running configuration"""
    ntp_servers = []
    ntp_dict = {}
    with open("/etc/ntp.conf") as ntp_file:
        data = ntp_file.readlines()
    for line in data:
        if line.startswith("server "):
            ntp_server = line.split(" ")[1]
            ntp_servers.append(ntp_server)
    ntp_dict['NTP Servers'] = ntp_servers
    print tabulate(ntp_dict, headers=ntp_dict.keys(), tablefmt="simple", stralign='left', missingval="")


# 'syslog' subcommand ("show runningconfiguration syslog")
@runningconfiguration.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def syslog(verbose):
    """Show Syslog running configuration"""
    syslog_servers = []
    syslog_dict = {}
    with open("/etc/rsyslog.conf") as syslog_file:
        data = syslog_file.readlines()
    for line in data:
        if line.startswith("*.* @"):
            line = line.split(":")
            server = line[0][5:]
            syslog_servers.append(server)
    syslog_dict['Syslog Servers'] = syslog_servers
    print tabulate(syslog_dict, headers=syslog_dict.keys(), tablefmt="simple", stralign='left', missingval="")


#
# 'startupconfiguration' group ("show startupconfiguration ...")
#

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def startupconfiguration():
    """Show startup configuration information"""
    pass


# 'bgp' subcommand  ("show startupconfiguration bgp")
@startupconfiguration.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def bgp(verbose):
    """Show BGP startup configuration"""
    cmd = "sudo docker ps | grep bgp | awk '{print$2}' | cut -d'-' -f3 | cut -d':' -f1"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    result = proc.stdout.read().rstrip()
    click.echo("Routing-Stack is: {}".format(result))
    if result == "quagga":
        run_command('sudo docker exec bgp cat /etc/quagga/bgpd.conf', display_cmd=verbose)
    elif result == "frr":
        run_command('sudo docker exec bgp cat /etc/frr/bgpd.conf', display_cmd=verbose)
    elif result == "gobgp":
        run_command('sudo docker exec bgp cat /etc/gpbgp/bgpd.conf', display_cmd=verbose)
    else:
        click.echo("Unidentified routing-stack")

#
# 'ntp' command ("show ntp")
#

@cli.command()
@click.pass_context
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def ntp(ctx, verbose):
    """Show NTP information"""
    ntpcmd = "ntpq -p -n"
    if is_mgmt_vrf_enabled(ctx) is True:
        #ManagementVRF is enabled. Call ntpq using cgexec
        ntpcmd = "cgexec -g l3mdev:mgmt ntpq -p -n"
    run_command(ntpcmd, display_cmd=verbose)



#
# 'uptime' command ("show uptime")
#

@cli.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def uptime(verbose):
    """Show system uptime"""
    cmd = "uptime -p"
    run_command(cmd, display_cmd=verbose)

@cli.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def clock(verbose):
    """Show date and time"""
    cmd ="date"
    run_command(cmd, display_cmd=verbose)

@cli.command('system-memory')
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def system_memory(verbose):
    """Show memory information"""
    cmd = "free -m"
    run_command(cmd, display_cmd=verbose)

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def vlan():
    """Show VLAN information"""
    pass

@vlan.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def brief(verbose):
    """Show all bridge information"""
    config_db = ConfigDBConnector()
    config_db.connect()
    header = ['VLAN ID', 'IP Address', 'Ports', 'Port Tagging', 'DHCP Helper Address']
    body = []
    vlan_keys = []

    # Fetching data from config_db for VLAN, VLAN_INTERFACE and VLAN_MEMBER
    vlan_dhcp_helper_data = config_db.get_table('VLAN')
    vlan_ip_data = config_db.get_table('VLAN_INTERFACE')
    vlan_ports_data = config_db.get_table('VLAN_MEMBER')

    vlan_keys = natsorted(vlan_dhcp_helper_data.keys())

    # Defining dictionaries for DHCP Helper address, Interface Gateway IP,
    # VLAN ports and port tagging
    vlan_dhcp_helper_dict = {}
    vlan_ip_dict = {}
    vlan_ports_dict = {}
    vlan_tagging_dict = {}

    # Parsing DHCP Helpers info
    for key in natsorted(vlan_dhcp_helper_data.keys()):
        try:
            if vlan_dhcp_helper_data[key]['dhcp_servers']:
                vlan_dhcp_helper_dict[str(key.strip('Vlan'))] = vlan_dhcp_helper_data[key]['dhcp_servers']
        except KeyError:
            vlan_dhcp_helper_dict[str(key.strip('Vlan'))] = " "
            pass

    # Parsing VLAN Gateway info
    for key in natsorted(vlan_ip_data.keys()):
        if not is_ip_prefix_in_key(key):
            continue
        interface_key = str(key[0].strip("Vlan"))
        interface_value = str(key[1])
        if interface_key in vlan_ip_dict:
            vlan_ip_dict[interface_key].append(interface_value)
        else:
            vlan_ip_dict[interface_key] = [interface_value]

    # Parsing VLAN Ports info
    for key in natsorted(vlan_ports_data.keys()):
        ports_key = str(key[0].strip("Vlan"))
        ports_value = str(key[1])
        ports_tagging = vlan_ports_data[key]['tagging_mode']
        if ports_key in vlan_ports_dict:
            if get_interface_mode() == "alias":
                ports_value = iface_alias_converter.name_to_alias(ports_value)
            vlan_ports_dict[ports_key].append(ports_value)
        else:
            if get_interface_mode() == "alias":
                ports_value = iface_alias_converter.name_to_alias(ports_value)
            vlan_ports_dict[ports_key] = [ports_value]
        if ports_key in vlan_tagging_dict:
            vlan_tagging_dict[ports_key].append(ports_tagging)
        else:
            vlan_tagging_dict[ports_key] = [ports_tagging]

    # Printing the following dictionaries in tablular forms:
    # vlan_dhcp_helper_dict={}, vlan_ip_dict = {}, vlan_ports_dict = {}
    # vlan_tagging_dict = {}
    for key in natsorted(vlan_dhcp_helper_dict.keys()):
        if key not in vlan_ip_dict:
            ip_address = ""
        else:
            ip_address = ','.replace(',', '\n').join(vlan_ip_dict[key])
        if key not in vlan_ports_dict:
            vlan_ports = ""
        else:
            vlan_ports = ','.replace(',', '\n').join((vlan_ports_dict[key]))
        if key not in vlan_dhcp_helper_dict:
            dhcp_helpers = ""
        else:
            dhcp_helpers = ','.replace(',', '\n').join(vlan_dhcp_helper_dict[key])
        if key not in vlan_tagging_dict:
            vlan_tagging = ""
        else:
            vlan_tagging = ','.replace(',', '\n').join((vlan_tagging_dict[key]))
        body.append([key, ip_address, vlan_ports, vlan_tagging, dhcp_helpers])
    click.echo(tabulate(body, header, tablefmt="grid"))

@vlan.command()
@click.option('-s', '--redis-unix-socket-path', help='unix socket path for redis connection')
def config(redis_unix_socket_path):
    kwargs = {}
    if redis_unix_socket_path:
        kwargs['unix_socket_path'] = redis_unix_socket_path
    config_db = ConfigDBConnector(**kwargs)
    config_db.connect(wait_for_init=False)
    data = config_db.get_table('VLAN')
    keys = data.keys()

    def tablelize(keys, data):
        table = []

        for k in natsorted(keys):
            if 'members' not in data[k] :
                r = []
                r.append(k)
                r.append(data[k]['vlanid'])
                table.append(r)
                continue

            for m in data[k].get('members', []):
                r = []
                r.append(k)
                r.append(data[k]['vlanid'])
                if get_interface_mode() == "alias":
                    alias = iface_alias_converter.name_to_alias(m)
                    r.append(alias)
                else:
                    r.append(m)

                entry = config_db.get_entry('VLAN_MEMBER', (k, m))
                mode = entry.get('tagging_mode')
                if mode == None:
                    r.append('?')
                else:
                    r.append(mode)

                table.append(r)

        return table

    header = ['Name', 'VID', 'Member', 'Mode']
    click.echo(tabulate(tablelize(keys, data), header))

@cli.command('services')
def services():
    """Show all daemon services"""
    cmd = "sudo docker ps --format '{{.Names}}'"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    while True:
        line = proc.stdout.readline()
        if line != '':
                print(line.rstrip()+'\t'+"docker")
                print("---------------------------")
                cmd = "sudo docker exec {} ps aux | sed '$d'".format(line.rstrip())
                proc1 = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
                print proc1.stdout.read()
        else:
                break

@cli.command()
def aaa():
    """Show AAA configuration"""
    config_db = ConfigDBConnector()
    config_db.connect()
    data = config_db.get_table('AAA')
    output = ''

    aaa = {
        'authentication': {
            'login': 'local (default)',
            'failthrough': 'False (default)'
        }
    }
    if 'authentication' in data:
        aaa['authentication'].update(data['authentication'])
    for row in aaa:
        entry = aaa[row]
        for key in entry:
            output += ('AAA %s %s %s\n' % (row, key, str(entry[key])))
    click.echo(output)


@cli.command()
def tacacs():
    """Show TACACS+ configuration"""
    config_db = ConfigDBConnector()
    config_db.connect()
    output = ''
    data = config_db.get_table('TACPLUS')

    tacplus = {
        'global': {
            'auth_type': 'pap (default)',
            'timeout': '5 (default)',
            'passkey': '<EMPTY_STRING> (default)'
        }
    }
    if 'global' in data:
        tacplus['global'].update(data['global'])
    for key in tacplus['global']:
        output += ('TACPLUS global %s %s\n' % (str(key), str(tacplus['global'][key])))

    data = config_db.get_table('TACPLUS_SERVER')
    if data != {}:
        for row in data:
            entry = data[row]
            output += ('\nTACPLUS_SERVER address %s\n' % row)
            for key in entry:
                output += ('               %s %s\n' % (key, str(entry[key])))
    click.echo(output)

#
# 'mirror_session' command  ("show mirror_session ...")
#
@cli.command('mirror_session')
@click.argument('session_name', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def mirror_session(session_name, verbose):
    """Show existing everflow sessions"""
    cmd = "acl-loader show session"

    if session_name is not None:
        cmd += " {}".format(session_name)

    run_command(cmd, display_cmd=verbose)


#
# 'policer' command  ("show policer ...")
#
@cli.command()
@click.argument('policer_name', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def policer(policer_name, verbose):
    """Show existing policers"""
    cmd = "acl-loader show policer"

    if policer_name is not None:
        cmd += " {}".format(policer_name)

    run_command(cmd, display_cmd=verbose)


#
# 'sflow command ("show sflow ...")
#
@cli.group(invoke_without_command=True)
@click.pass_context
def sflow(ctx):
    """Show sFlow related information"""
    config_db = ConfigDBConnector()
    config_db.connect()
    ctx.obj = {'db': config_db}
    if ctx.invoked_subcommand is None:
        show_sflow_global(config_db)

#
# 'sflow command ("show sflow interface ...")
#
@sflow.command('interface')
@click.pass_context
def sflow_interface(ctx):
    """Show sFlow interface information"""
    show_sflow_interface(ctx.obj['db'])

def sflow_appDB_connect():
    db = SonicV2Connector(host='127.0.0.1')
    db.connect(db.APPL_DB, False)
    return db

def show_sflow_interface(config_db):
    sess_db = sflow_appDB_connect()
    if not sess_db:
        click.echo("sflow AppDB error")
        return

    port_tbl = config_db.get_table('PORT')
    if not port_tbl:
        click.echo("No ports configured")
        return

    click.echo("\nsFlow interface configurations")
    header = ['Interface', 'Admin State', 'Sampling Rate']
    body = []
    for pname in natsorted(port_tbl.keys()):
        intf_key = 'SFLOW_SESSION_TABLE:' + pname
        sess_info = sess_db.get_all(sess_db.APPL_DB, intf_key)
        if sess_info is None:
            continue
        body_info = [pname]
        body_info.append(sess_info['admin_state'])
        body_info.append(sess_info['sample_rate'])
        body.append(body_info)
    click.echo(tabulate(body, header, tablefmt='grid'))

def show_sflow_global(config_db):

    sflow_info = config_db.get_table('SFLOW')
    global_admin_state = 'down'
    if sflow_info:
        global_admin_state = sflow_info['global']['admin_state']

    click.echo("\nsFlow Global Information:")
    click.echo("  sFlow Admin State:".ljust(30) + "{}".format(global_admin_state))


    click.echo("  sFlow Polling Interval:".ljust(30), nl=False)
    if (sflow_info and 'polling_interval' in sflow_info['global'].keys()):
        click.echo("{}".format(sflow_info['global']['polling_interval']))
    else:
        click.echo("default")

    click.echo("  sFlow AgentID:".ljust(30), nl=False)
    if (sflow_info and 'agent_id' in sflow_info['global'].keys()):
        click.echo("{}".format(sflow_info['global']['agent_id']))
    else:
        click.echo("default")

    sflow_info = config_db.get_table('SFLOW_COLLECTOR')
    click.echo("\n  {} Collectors configured:".format(len(sflow_info)))
    for collector_name in sorted(sflow_info.keys()):
        click.echo("    Name: {}".format(collector_name).ljust(30) +
                   "IP addr: {}".format(sflow_info[collector_name]['collector_ip']).ljust(20) +
                   "UDP port: {}".format(sflow_info[collector_name]['collector_port']))


#
# 'acl' group ###
#

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def acl():
    """Show ACL related information"""
    pass


# 'rule' subcommand  ("show acl rule")
@acl.command()
@click.argument('table_name', required=False)
@click.argument('rule_id', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def rule(table_name, rule_id, verbose):
    """Show existing ACL rules"""
    cmd = "acl-loader show rule"

    if table_name is not None:
        cmd += " {}".format(table_name)

    if rule_id is not None:
        cmd += " {}".format(rule_id)

    run_command(cmd, display_cmd=verbose)


# 'table' subcommand  ("show acl table")
@acl.command()
@click.argument('table_name', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def table(table_name, verbose):
    """Show existing ACL tables"""
    cmd = "acl-loader show table"

    if table_name is not None:
        cmd += " {}".format(table_name)

    run_command(cmd, display_cmd=verbose)


#
# 'dropcounters' group ###
#

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def dropcounters():
    """Show drop counter related information"""
    pass


# 'configuration' subcommand ("show dropcounters configuration")
@dropcounters.command()
@click.option('-g', '--group', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def configuration(group, verbose):
    """Show current drop counter configuration"""
    cmd = "dropconfig -c show_config"

    if group:
        cmd += " -g '{}'".format(group)

    run_command(cmd, display_cmd=verbose)


# 'capabilities' subcommand ("show dropcounters capabilities")
@dropcounters.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def capabilities(verbose):
    """Show device drop counter capabilities"""
    cmd = "dropconfig -c show_capabilities"

    run_command(cmd, display_cmd=verbose)


# 'counts' subcommand ("show dropcounters counts")
@dropcounters.command()
@click.option('-g', '--group', required=False)
@click.option('-t', '--counter_type', required=False)
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def counts(group, counter_type, verbose):
    """Show drop counts"""
    cmd = "dropstat -c show"

    if group:
        cmd += " -g '{}'".format(group)

    if counter_type:
        cmd += " -t '{}'".format(counter_type)

    run_command(cmd, display_cmd=verbose)


#
# 'ecn' command ("show ecn")
#
@cli.command('ecn')
def ecn():
    """Show ECN configuration"""
    cmd = "ecnconfig -l"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    click.echo(proc.stdout.read())


#
# 'boot' command ("show boot")
#
@cli.command('boot')
def boot():
    """Show boot configuration"""
    cmd = "sudo sonic_installer list"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    click.echo(proc.stdout.read())


# 'mmu' command ("show mmu")
#
@cli.command('mmu')
def mmu():
    """Show mmu configuration"""
    cmd = "mmuconfig -l"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    click.echo(proc.stdout.read())


#
# 'reboot-cause' command ("show reboot-cause")
#
@cli.command('reboot-cause')
def reboot_cause():
    """Show cause of most recent reboot"""
    PREVIOUS_REBOOT_CAUSE_FILE = "/host/reboot-cause/previous-reboot-cause.txt"

    # At boot time, PREVIOUS_REBOOT_CAUSE_FILE is generated based on
    # the contents of the 'reboot cause' file as it was left when the device
    # went down for reboot. This file should always be created at boot,
    # but check first just in case it's not present.
    if not os.path.isfile(PREVIOUS_REBOOT_CAUSE_FILE):
        click.echo("Unable to determine cause of previous reboot\n")
    else:
        cmd = "cat {}".format(PREVIOUS_REBOOT_CAUSE_FILE)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
        click.echo(proc.stdout.read())


#
# 'line' command ("show line")
#
@cli.command('line')
def line():
    """Show all /dev/ttyUSB lines and their info"""
    cmd = "consutil show"
    run_command(cmd, display_cmd=verbose)
    return


@cli.group(name='warm_restart', cls=AliasedGroup, default_if_no_args=False)
def warm_restart():
    """Show warm restart configuration and state"""
    pass

@warm_restart.command()
@click.option('-s', '--redis-unix-socket-path', help='unix socket path for redis connection')
def state(redis_unix_socket_path):
    """Show warm restart state"""
    kwargs = {}
    if redis_unix_socket_path:
        kwargs['unix_socket_path'] = redis_unix_socket_path

    data = {}
    db = SonicV2Connector(host='127.0.0.1')
    db.connect(db.STATE_DB, False)   # Make one attempt only

    TABLE_NAME_SEPARATOR = '|'
    prefix = 'WARM_RESTART_TABLE' + TABLE_NAME_SEPARATOR
    _hash = '{}{}'.format(prefix, '*')
    table_keys = db.keys(db.STATE_DB, _hash)

    def remove_prefix(text, prefix):
        if text.startswith(prefix):
            return text[len(prefix):]
        return text

    table = []
    for tk in table_keys:
        entry = db.get_all(db.STATE_DB, tk)
        r = []
        r.append(remove_prefix(tk, prefix))
        if 'restore_count' not in entry:
            r.append("")
        else:
            r.append(entry['restore_count'])

        if 'state' not in entry:
            r.append("")
        else:
            r.append(entry['state'])

        table.append(r)

    header = ['name', 'restore_count', 'state']
    click.echo(tabulate(table, header))

@warm_restart.command()
@click.option('-s', '--redis-unix-socket-path', help='unix socket path for redis connection')
def config(redis_unix_socket_path):
    """Show warm restart config"""
    kwargs = {}
    if redis_unix_socket_path:
        kwargs['unix_socket_path'] = redis_unix_socket_path
    config_db = ConfigDBConnector(**kwargs)
    config_db.connect(wait_for_init=False)
    data = config_db.get_table('WARM_RESTART')
    # Python dictionary keys() Method
    keys = data.keys()

    state_db = SonicV2Connector(host='127.0.0.1')
    state_db.connect(state_db.STATE_DB, False)   # Make one attempt only
    TABLE_NAME_SEPARATOR = '|'
    prefix = 'WARM_RESTART_ENABLE_TABLE' + TABLE_NAME_SEPARATOR
    _hash = '{}{}'.format(prefix, '*')
    # DBInterface keys() method
    enable_table_keys = state_db.keys(state_db.STATE_DB, _hash)

    def tablelize(keys, data, enable_table_keys, prefix):
        table = []

        if enable_table_keys is not None:
            for k in enable_table_keys:
                k = k.replace(prefix, "")
                if k not in keys:
                    keys.append(k)

        for k in keys:
            r = []
            r.append(k)

            enable_k = prefix + k
            if enable_table_keys is None or enable_k not in enable_table_keys:
                r.append("false")
            else:
                r.append(state_db.get(state_db.STATE_DB, enable_k, "enable"))

            if k not in data:
                r.append("NULL")
                r.append("NULL")
                r.append("NULL")
            elif 'neighsyncd_timer' in  data[k]:
                r.append("neighsyncd_timer")
                r.append(data[k]['neighsyncd_timer'])
                r.append("NULL")
            elif 'bgp_timer' in data[k] or 'bgp_eoiu' in data[k]:
                if 'bgp_timer' in data[k]:
                    r.append("bgp_timer")
                    r.append(data[k]['bgp_timer'])
                else:
                    r.append("NULL")
                    r.append("NULL")
                if 'bgp_eoiu' in data[k]:
                    r.append(data[k]['bgp_eoiu'])
                else:
                    r.append("NULL")
            elif 'teamsyncd_timer' in data[k]:
                r.append("teamsyncd_timer")
                r.append(data[k]['teamsyncd_timer'])
                r.append("NULL")
            else:
                r.append("NULL")
                r.append("NULL")
                r.append("NULL")

            table.append(r)

        return table

    header = ['name', 'enable', 'timer_name', 'timer_duration', 'eoiu_enable']
    click.echo(tabulate(tablelize(keys, data, enable_table_keys, prefix), header))
    state_db.close(state_db.STATE_DB)

#
# 'nat' group ("show nat ...")
#

@cli.group(cls=AliasedGroup, default_if_no_args=False)
def nat():
    """Show details of the nat """
    pass

# 'statistics' subcommand ("show nat statistics")
@nat.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def statistics(verbose):
    """ Show NAT statistics """

    cmd = "sudo natshow -s"
    run_command(cmd, display_cmd=verbose)

# 'translations' subcommand ("show nat translations")
@nat.group(invoke_without_command=True)
@click.pass_context
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def translations(ctx, verbose):
    """ Show NAT translations """

    if ctx.invoked_subcommand is None:
        cmd = "sudo natshow -t"
        run_command(cmd, display_cmd=verbose)

# 'count' subcommand ("show nat translations count")
@translations.command()
def count():
    """ Show NAT translations count """

    cmd = "sudo natshow -c"
    run_command(cmd)

# 'config' subcommand ("show nat config")
@nat.group(invoke_without_command=True)
@click.pass_context
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def config(ctx, verbose):
    """Show NAT config related information"""
    if ctx.invoked_subcommand is None:
        click.echo("\nGlobal Values")
        cmd = "sudo natconfig -g"
        run_command(cmd, display_cmd=verbose)
        click.echo("Static Entries")
        cmd = "sudo natconfig -s"
        run_command(cmd, display_cmd=verbose)
        click.echo("Pool Entries")
        cmd = "sudo natconfig -p"
        run_command(cmd, display_cmd=verbose)
        click.echo("NAT Bindings")
        cmd = "sudo natconfig -b"
        run_command(cmd, display_cmd=verbose)
        click.echo("NAT Zones")
        cmd = "sudo natconfig -z"
        run_command(cmd, display_cmd=verbose)

# 'static' subcommand  ("show nat config static")
@config.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def static(verbose):
    """Show static NAT configuration"""

    cmd = "sudo natconfig -s"
    run_command(cmd, display_cmd=verbose)

# 'pool' subcommand  ("show nat config pool")
@config.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def pool(verbose):
    """Show NAT Pool configuration"""

    cmd = "sudo natconfig -p"
    run_command(cmd, display_cmd=verbose)


# 'bindings' subcommand  ("show nat config bindings")
@config.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def bindings(verbose):
    """Show NAT binding configuration"""

    cmd = "sudo natconfig -b"
    run_command(cmd, display_cmd=verbose)

# 'globalvalues' subcommand  ("show nat config globalvalues")
@config.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def globalvalues(verbose):
    """Show NAT Global configuration"""

    cmd = "sudo natconfig -g"
    run_command(cmd, display_cmd=verbose)

# 'zones' subcommand  ("show nat config zones")
@config.command()
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def zones(verbose):
    """Show NAT Zone configuration"""

    cmd = "sudo natconfig -z"
    run_command(cmd, display_cmd=verbose)

#
# 'ztp status' command ("show ztp status")
#
@cli.command()
@click.argument('status', required=False, type=click.Choice(["status"]))
@click.option('--verbose', is_flag=True, help="Enable verbose output")
def ztp(status, verbose):
    """Show Zero Touch Provisioning status"""
    if os.path.isfile('/usr/bin/ztp') is False:
        exit("ZTP feature unavailable in this image version")

    if os.geteuid() != 0:
        exit("Root privileges are required for this operation")
    pass

    cmd = "ztp status"
    if verbose:
       cmd = cmd + " --verbose"
    run_command(cmd, display_cmd=verbose)

#
# 'feature' group (show feature ...)
#
@cli.group(name='feature', invoke_without_command=False)
def feature():
    """Show feature status"""
    pass

#
# 'state' subcommand (show feature status)
#
@feature.command('status', short_help="Show feature state")
@click.argument('feature_name', required=False)
def autorestart(feature_name):
    header = ['Feature', 'State', 'AutoRestart']
    body = []
    feature_table = config_db.get_table('FEATURE')
    if feature_name:
        if feature_table and feature_table.has_key(feature_name):
            body.append([feature_name, feature_table[feature_name]['state'], \
                         feature_table[feature_name]['auto_restart']])
        else:
            click.echo("Can not find feature {}".format(feature_name))
            sys.exit(1)
    else:
        for key in natsorted(feature_table.keys()):
            body.append([key, feature_table[key]['state'], feature_table[key]['auto_restart']])
    click.echo(tabulate(body, header))

#
# 'autorestart' subcommand (show feature autorestart)
#
@feature.command('autorestart', short_help="Show auto-restart state for a feature")
@click.argument('feature_name', required=False)
def autorestart(feature_name):
    header = ['Feature', 'AutoRestart']
    body = []
    feature_table = config_db.get_table('FEATURE')
    if feature_name:
        if feature_table and feature_table.has_key(feature_name):
            body.append([feature_name, feature_table[feature_name]['auto_restart']])
        else:
            click.echo("Can not find feature {}".format(feature_name))
            sys.exit(1)
    else:
        for name in natsorted(feature_table.keys()):
            body.append([name, feature_table[name]['auto_restart']])
    click.echo(tabulate(body, header))

#
# 'vnet' command ("show vnet")
#
@cli.group(cls=AliasedGroup, default_if_no_args=False)
def vnet():
    """Show vnet related information"""
    pass

@vnet.command()
@click.argument('vnet_name', required=True)
def name(vnet_name):
    """Show vnet name <vnet name> information"""
    config_db = ConfigDBConnector()
    config_db.connect()
    header = ['vnet name', 'vxlan tunnel', 'vni', 'peer list']

    # Fetching data from config_db for VNET
    vnet_data = config_db.get_entry('VNET', vnet_name)

    def tablelize(vnet_key, vnet_data):
        table = []
        if vnet_data:
            r = []
            r.append(vnet_key)
            r.append(vnet_data.get('vxlan_tunnel'))
            r.append(vnet_data.get('vni'))
            r.append(vnet_data.get('peer_list'))
            table.append(r)
        return table

    click.echo(tabulate(tablelize(vnet_name, vnet_data), header))

@vnet.command()
def brief():
    """Show vnet brief information"""
    config_db = ConfigDBConnector()
    config_db.connect()
    header = ['vnet name', 'vxlan tunnel', 'vni', 'peer list']

    # Fetching data from config_db for VNET
    vnet_data = config_db.get_table('VNET')
    vnet_keys = natsorted(vnet_data.keys())

    def tablelize(vnet_keys, vnet_data):
        table = []
        for k in vnet_keys:
            r = []
            r.append(k)
            r.append(vnet_data[k].get('vxlan_tunnel'))
            r.append(vnet_data[k].get('vni'))
            r.append(vnet_data[k].get('peer_list'))
            table.append(r)
        return table

    click.echo(tabulate(tablelize(vnet_keys, vnet_data), header))

@vnet.command()
@click.argument('vnet_alias', required=False)
def alias(vnet_alias):
    """Show vnet alias to name information"""
    config_db = ConfigDBConnector()
    config_db.connect()
    header = ['Alias', 'Name']

    # Fetching data from config_db for VNET
    vnet_data = config_db.get_table('VNET')
    vnet_keys = natsorted(vnet_data.keys())

    def tablelize(vnet_keys, vnet_data, vnet_alias):
        table = []
        for k in vnet_keys:
            r = []
            if vnet_alias is not None:
                if vnet_data[k].get('guid') == vnet_alias:
                    r.append(vnet_data[k].get('guid'))
                    r.append(k)
                    table.append(r)
                    return table
                else:
                    continue

            r.append(vnet_data[k].get('guid'))
            r.append(k)
            table.append(r)
        return table

    click.echo(tabulate(tablelize(vnet_keys, vnet_data, vnet_alias), header))

@vnet.command()
def interfaces():
    """Show vnet interfaces information"""
    config_db = ConfigDBConnector()
    config_db.connect()

    header = ['vnet name', 'interfaces']

    # Fetching data from config_db for interfaces
    intfs_data = config_db.get_table("INTERFACE")
    vlan_intfs_data = config_db.get_table("VLAN_INTERFACE")

    vnet_intfs = {}
    for k, v in intfs_data.items():
        if 'vnet_name' in v:
            vnet_name = v['vnet_name']
            if vnet_name in vnet_intfs:
                vnet_intfs[vnet_name].append(k)
            else:
                vnet_intfs[vnet_name] = [k]

    for k, v in vlan_intfs_data.items():
        if 'vnet_name' in v:
            vnet_name = v['vnet_name']
            if vnet_name in vnet_intfs:
                vnet_intfs[vnet_name].append(k)
            else:
                vnet_intfs[vnet_name] = [k]

    table = []
    for k, v in vnet_intfs.items():
        r = []
        r.append(k)
        r.append(",".join(natsorted(v)))
        table.append(r)

    click.echo(tabulate(table, header))

@vnet.command()
def neighbors():
    """Show vnet neighbors information"""
    config_db = ConfigDBConnector()
    config_db.connect()

    header = ['<vnet_name>', 'neighbor', 'mac_address', 'interfaces']

    # Fetching data from config_db for interfaces
    intfs_data = config_db.get_table("INTERFACE")
    vlan_intfs_data = config_db.get_table("VLAN_INTERFACE")

    vnet_intfs = {}
    for k, v in intfs_data.items():
        if 'vnet_name' in v:
            vnet_name = v['vnet_name']
            if vnet_name in vnet_intfs:
                vnet_intfs[vnet_name].append(k)
            else:
                vnet_intfs[vnet_name] = [k]

    for k, v in vlan_intfs_data.items():
        if 'vnet_name' in v:
            vnet_name = v['vnet_name']
            if vnet_name in vnet_intfs:
                vnet_intfs[vnet_name].append(k)
            else:
                vnet_intfs[vnet_name] = [k]

    appl_db = SonicV2Connector()
    appl_db.connect(appl_db.APPL_DB)

    # Fetching data from appl_db for neighbors
    nbrs = appl_db.keys(appl_db.APPL_DB, "NEIGH_TABLE*")
    nbrs_data = {}
    for nbr in nbrs if nbrs else []:
        tbl, intf, ip = nbr.split(":", 2)
        mac = appl_db.get(appl_db.APPL_DB, nbr, 'neigh')
        if intf in nbrs_data:
            nbrs_data[intf].append((ip, mac))
        else:
            nbrs_data[intf] = [(ip, mac)]

    table = []
    for k, v in vnet_intfs.items():
        v = natsorted(v)
        header[0] = k
        table = []
        for intf in v:
            if intf in nbrs_data:
                for ip, mac in nbrs_data[intf]:
                    r = ["", ip, mac, intf]
                    table.append(r)
        click.echo(tabulate(table, header))
        click.echo("\n")

    if not bool(vnet_intfs):
        click.echo(tabulate(table, header))

@vnet.group()
def routes():
    """Show vnet routes related information"""
    pass

@routes.command()
def all():
    """Show all vnet routes"""
    appl_db = SonicV2Connector()
    appl_db.connect(appl_db.APPL_DB)

    header = ['vnet name', 'prefix', 'nexthop', 'interface']

    # Fetching data from appl_db for VNET ROUTES
    vnet_rt_keys = appl_db.keys(appl_db.APPL_DB, "VNET_ROUTE_TABLE*")
    vnet_rt_keys = natsorted(vnet_rt_keys) if vnet_rt_keys else []

    table = []
    for k in vnet_rt_keys:
        r = []
        r.extend(k.split(":", 2)[1:])
        val = appl_db.get_all(appl_db.APPL_DB, k)
        r.append(val.get('nexthop'))
        r.append(val.get('ifname'))
        table.append(r)

    click.echo(tabulate(table, header))

    click.echo("\n")

    header = ['vnet name', 'prefix', 'endpoint', 'mac address', 'vni']

    # Fetching data from appl_db for VNET TUNNEL ROUTES
    vnet_rt_keys = appl_db.keys(appl_db.APPL_DB, "VNET_ROUTE_TUNNEL_TABLE*")
    vnet_rt_keys = natsorted(vnet_rt_keys) if vnet_rt_keys else []

    table = []
    for k in vnet_rt_keys:
        r = []
        r.extend(k.split(":", 2)[1:])
        val = appl_db.get_all(appl_db.APPL_DB, k)
        r.append(val.get('endpoint'))
        r.append(val.get('mac_address'))
        r.append(val.get('vni'))
        table.append(r)

    click.echo(tabulate(table, header))

@routes.command()
def tunnel():
    """Show vnet tunnel routes"""
    appl_db = SonicV2Connector()
    appl_db.connect(appl_db.APPL_DB)

    header = ['vnet name', 'prefix', 'endpoint', 'mac address', 'vni']

    # Fetching data from appl_db for VNET TUNNEL ROUTES
    vnet_rt_keys = appl_db.keys(appl_db.APPL_DB, "VNET_ROUTE_TUNNEL_TABLE*")
    vnet_rt_keys = natsorted(vnet_rt_keys) if vnet_rt_keys else []

    table = []
    for k in vnet_rt_keys:
        r = []
        r.extend(k.split(":", 2)[1:])
        val = appl_db.get_all(appl_db.APPL_DB, k)
        r.append(val.get('endpoint'))
        r.append(val.get('mac_address'))
        r.append(val.get('vni'))
        table.append(r)

    click.echo(tabulate(table, header))

#
# 'vxlan' command ("show vxlan")
#
@cli.group(cls=AliasedGroup, default_if_no_args=False)
def vxlan():
    """Show vxlan related information"""
    pass

@vxlan.command()
@click.argument('vxlan_name', required=True)
def name(vxlan_name):
    """Show vxlan name <vxlan_name> information"""
    config_db = ConfigDBConnector()
    config_db.connect()
    header = ['vxlan tunnel name', 'source ip', 'destination ip', 'tunnel map name', 'tunnel map mapping(vni -> vlan)']

    # Fetching data from config_db for VXLAN TUNNEL
    vxlan_data = config_db.get_entry('VXLAN_TUNNEL', vxlan_name)

    table = []
    if vxlan_data:
        r = []
        r.append(vxlan_name)
        r.append(vxlan_data.get('src_ip'))
        r.append(vxlan_data.get('dst_ip'))
        vxlan_map_keys = config_db.keys(config_db.CONFIG_DB,
                        'VXLAN_TUNNEL_MAP{}{}{}*'.format(config_db.KEY_SEPARATOR, vxlan_name, config_db.KEY_SEPARATOR))
        if vxlan_map_keys:
            vxlan_map_mapping = config_db.get_all(config_db.CONFIG_DB, vxlan_map_keys[0])
            r.append(vxlan_map_keys[0].split(config_db.KEY_SEPARATOR, 2)[2])
            r.append("{} -> {}".format(vxlan_map_mapping.get('vni'), vxlan_map_mapping.get('vlan')))
        table.append(r)

    click.echo(tabulate(table, header))

@vxlan.command()
def tunnel():
    """Show vxlan tunnel information"""
    config_db = ConfigDBConnector()
    config_db.connect()
    header = ['vxlan tunnel name', 'source ip', 'destination ip', 'tunnel map name', 'tunnel map mapping(vni -> vlan)']

    # Fetching data from config_db for VXLAN TUNNEL
    vxlan_data = config_db.get_table('VXLAN_TUNNEL')
    vxlan_keys = natsorted(vxlan_data.keys())

    table = []
    for k in vxlan_keys:
        r = []
        r.append(k)
        r.append(vxlan_data[k].get('src_ip'))
        r.append(vxlan_data[k].get('dst_ip'))
        vxlan_map_keys = config_db.keys(config_db.CONFIG_DB,
                        'VXLAN_TUNNEL_MAP{}{}{}*'.format(config_db.KEY_SEPARATOR,k, config_db.KEY_SEPARATOR))
        if vxlan_map_keys:
            vxlan_map_mapping = config_db.get_all(config_db.CONFIG_DB, vxlan_map_keys[0])
            r.append(vxlan_map_keys[0].split(config_db.KEY_SEPARATOR, 2)[2])
            r.append("{} -> {}".format(vxlan_map_mapping.get('vni'), vxlan_map_mapping.get('vlan')))
        table.append(r)

    click.echo(tabulate(table, header))

if __name__ == '__main__':
    cli()
