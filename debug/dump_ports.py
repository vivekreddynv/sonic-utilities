import click

from swsscommon import swsscommon
from tabulate import tabulate

def get_all_ports(): 
    db = swsscommon.DBConnector("APPL_DB", 0)
    tbl = swsscommon.Table(db, "PORT_TABLE")
    return [p for p in tbl.getKeys() if "Ethernet" in p]

def print_table(fvs):
    header = ["Key", "Value"]
    return tabulate(fvs, header)

def get_appl_info(port):
    db = swsscommon.DBConnector("APPL_DB", 0)
    tbl = swsscommon.Table(db, "PORT_TABLE")
    return tbl.get(port)[1]

def get_config_info(port):
    db = swsscommon.DBConnector("CONFIG_DB", 0)
    tbl = swsscommon.Table(db, "PORT")
    return tbl.get(port)[1]

def get_asic_info(port):
    db = swsscommon.DBConnector("ASIC_DB", 0)
    tbl = swsscommon.Table(db, "ASIC_STATE:SAI_OBJECT_TYPE_HOSTIF")
    for key in tbl.getKeys():
        val = dict(tbl.get(key)[1])
        if val["SAI_HOSTIF_ATTR_NAME"] == port:
            ifmap = val["SAI_HOSTIF_ATTR_OBJ_ID"]
    tbl = swsscommon.Table(db, "ASIC_STATE:SAI_OBJECT_TYPE_PORT")
    return (("KEY", ifmap), *tbl.get(ifmap)[1])

def resolve_alias(port):
    if "Ethernet" in port:
        return port

    for p in get_all_ports():
        if dict(get_appl_info(p)).get("alias") == port:
            return p

    #TODO: Better error handling
    return port

def print_port(port):
    port = resolve_alias(port)
    print("----- {} -----".format(port))
    print()
    print("Application DB")
    print(print_table(get_appl_info(port)))
    print()
    print("Config DB")
    print(print_table(get_config_info(port)))
    print()
    print("ASIC DB")
    print(print_table(get_asic_info(port)))
    print()
    print()

#
# Debug ports by dumping APPL_DB CONFIG_DB and ASIC_DB data
#
@click.command()
@click.argument('port_name', required=False)
def ports(port_name):
    """Show debug information for the given (or all) ports"""
    ports = []
    if port_name is None:
        ports = get_all_ports()
        ports.sort(key=lambda pname: int(pname.split("Ethernet")[1]))
    else:
        ports = [port_name]

    for p in ports:
        print_port(p)


