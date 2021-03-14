
import click
import json 

from swsscommon import swsscommon
from . import copp_helper as SONiC


@click.group()
def config():
    "Dump Config DB info"
    pass


@config.command()
def trap():
    'Dump COPP_TRAP table'
    db = swsscommon.DBConnector("CONFIG_DB", 0)
    tbl = swsscommon.Table(db, SONiC.CFG_COPP_TRAP_TABLE_NAME)
    dump_dict = dict()
    final_dict = dict()
    for key in tbl.getKeys():
        dump = dict(tbl.get(key)[1])
        dump_dict[key] = dump
    final_dict[SONiC.CFG_COPP_TRAP_TABLE_NAME] = dump_dict
    click.echo(json.dumps(final_dict, indent=4))
    return 
        

@config.command()
def group():
    'Dump COPP_GROUP table'
    db = swsscommon.DBConnector("CONFIG_DB", 0)
    tbl = swsscommon.Table(db, SONiC.CFG_COPP_GROUP_TABLE_NAME)
    dump_dict = dict()
    final_dict = dict()
    for key in tbl.getKeys():
        dump = dict(tbl.get(key)[1])
        dump_dict[key] = dump
    final_dict[SONiC.CFG_COPP_GROUP_TABLE_NAME] = dump_dict
    click.echo(json.dumps(final_dict, indent=4))
    return 

@click.command()
def appl():
    'Dump COPP_TABLE table in APPL_DB'
    db = swsscommon.DBConnector("APPL_DB", 0)
    tbl = swsscommon.Table(db, SONiC.APP_COPP_TABLE_NAME)
    dump_dict = dict()
    final_dict = dict()
    for key in tbl.getKeys():
        dump = dict(tbl.get(key)[1])
        dump_dict[key] = dump
    final_dict[SONiC.APP_COPP_TABLE_NAME] = dump_dict
    click.echo(json.dumps(final_dict, indent=4))
    return 

