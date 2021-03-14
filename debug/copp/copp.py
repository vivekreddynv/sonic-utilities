import click
import sys, os

from . import dump_trap
from . import dump_db

@click.group()
def copp():
    """ Show debug/dump info for a copp construct """
    pass
    
copp.add_command(dump_trap.dump)
copp.add_command(dump_db.config)
copp.add_command(dump_db.appl)





