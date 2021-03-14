'''
Dumps Relevant Trap information from APPL_DB & ASIC_DB
'''

import click
import json
from swsscommon import swsscommon
from . import copp_helper as SONiC


'''
Trap Related Information
'''

@click.command()
@click.pass_context
@click.argument('trap_id', required=True, type=str)
def dump(ctx, trap_id):
    '''Dump a Trap Related Information \n
    
    Required: Trap Id to dump the Data of'''
    
    if trap_id not in SONiC.trap_id_map:
        ctx.fail("trap_id is not valid")
        
    trap_meta = TrapMeta(trap_id)
    
    ctx.conf_db = swsscommon.DBConnector("CONFIG_DB", 0)
    dump_str = trap_meta.fetch_from_conf_db(ctx)
    if dump_str is not None:
        click.echo(''' \n ----------- Config DB dump ------------ \n ''' + dump_str)
    else:
        click.echo("Trap Id not found in Config DB") 
    
    
    ctx.appl_db = swsscommon.DBConnector("APPL_DB", 0)
    dump_str = trap_meta.fetch_from_appl_db(ctx)
    if dump_str is not None:
        click.echo(''' \n ----------- Appl DB dump ------------ \n ''' + dump_str)
    else:
        click.echo("Relevant Copp Table not found in APPL DB") 
    
    if trap_meta.CONF_TRAP_KEY is None:
        return 
    
    ctx.asic_db = swsscommon.DBConnector("ASIC_DB", 0)
    dump_str = trap_meta.fetch_from_asic_db(ctx)
    if dump_str is not None:
        click.echo(''' \n ----------- ASIC DB dump ------------ \n ''' + dump_str)
    else:
        click.echo("Relevant SAI Objects not found in ASIC DB") 


class TrapMeta():
    def __init__(self, trap_id):
        self.trap_id = trap_id
        
        self.trap_map = {v: k for k, v in SONiC.trap_id_map.items()}
        
        self.CONF_TRAP_KEY = None
        self.CONF_TRAP_HASH = {}
        self.CONF_GROUP_KEY = None
        self.CONF_GROUP_HASH = {}
    
        self.SAI_TRAP = None
        self.SAI_GROUP = None
        self.SAI_POLICER = None
        self.SAI_QUEUE = None
    
    
    def fetch_from_conf_db(self, ctx):
    
        tbl = swsscommon.Table(ctx.conf_db, SONiC.CFG_COPP_TRAP_TABLE_NAME)
        
        for key in tbl.getKeys():
               
            if self.trap_id == key:
                self.CONF_TRAP_KEY = key
                break
            else:
                ids = tbl.hget(key, "trap_ids")[-1]
                if self.trap_id in ids:
                    self.CONF_TRAP_KEY = key
                    break
        
        if self.CONF_TRAP_KEY is not None:
            self.CONF_GROUP_KEY = tbl.hget(self.CONF_TRAP_KEY, "trap_group")[-1]
            self.CONF_TRAP_HASH = dict(tbl.get(self.CONF_TRAP_KEY)[1])
            grp_tbl = swsscommon.Table(ctx.conf_db, SONiC.CFG_COPP_GROUP_TABLE_NAME)
            self.CONF_GROUP_HASH = dict(grp_tbl.get(self.CONF_GROUP_KEY)[1])
            
            dict_final = {tbl.getTableName() + ":" + self.CONF_TRAP_KEY : self.CONF_TRAP_HASH, 
                            grp_tbl.getTableName() + ":" + self.CONF_GROUP_KEY : self.CONF_GROUP_HASH}
            
            return json.dumps(dict_final, indent=7)
        
        return None
    
       
    def fetch_from_appl_db(self, ctx):
        
        tbl = swsscommon.Table(ctx.appl_db, SONiC.APP_COPP_TABLE_NAME)
        
        self.APP_TBL_HASH = None
        
        if self.CONF_GROUP_KEY is not None:
            self.APP_TBL_HASH = dict(tbl.get(self.CONF_GROUP_KEY)[1])
        elif self.CONF_TRAP_KEY: # Looks for a stale entry in APPL DB 
            for key in tbl.getKeys():
                ids = tbl.hget(key, "trap_ids")[-1]
                if ids and self.CONF_TRAP_KEY in ids:
                    self.APP_TBL_HASH = dict(tbl.get(key)[1])
                    self.CONF_GROUP_KEY = key
                    break 
                
        if self.APP_TBL_HASH is not None:
            dict_final = {tbl.getTableName() + ":" + self.CONF_GROUP_KEY : self.APP_TBL_HASH}
            return json.dumps(dict_final, indent=7)

        return None
    
          
    def fetch_from_asic_db(self, ctx):
        self.dump_dict = dict()
        self.__fetch_trap_sai(ctx)
        self.__fetch_trap_group_sai(ctx)
        self.__fetch_policier_sai(ctx)    
        self.__fetch_queue_sai(ctx)
        return json.dumps(self.dump_dict, indent=7)
    
    def __fetch_trap_sai(self, ctx):
        
        tbl = swsscommon.Table(ctx.asic_db, SONiC.ASIC_TRAP_OBJ)
        temp_dump = {"Trap SAI Obj" : "Not Found"}
        
        for key in tbl.getKeys():
            sai_trap_type = tbl.hget(key, "SAI_HOSTIF_TRAP_ATTR_TRAP_TYPE")[-1]

            if self.trap_id == self.trap_map[sai_trap_type]:
                self.SAI_TRAP = key
                self.SAI_GROUP = tbl.hget(key, "SAI_HOSTIF_TRAP_ATTR_TRAP_GROUP")[-1]
                temp_dump = dict(tbl.get(key)[1])
                break
         
        self.dump_dict[SONiC.ASIC_TRAP_OBJ + ":" + self.SAI_TRAP] =  temp_dump

    
    def __fetch_trap_group_sai(self, ctx):
        
        tbl = swsscommon.Table(ctx.asic_db, SONiC.ASIC_TRAP_GROUP_OBJ)
        temp_dump = {"Trap Group SAI Object" : "Not Found"}
        
        if self.SAI_GROUP is not  None:
            temp_dump = dict(tbl.get(self.SAI_GROUP)[1])
            self.SAI_POLICER = tbl.hget(self.SAI_GROUP, "SAI_HOSTIF_TRAP_GROUP_ATTR_POLICER")[-1]
            self.SAI_QUEUE = tbl.hget(self.SAI_GROUP, "SAI_HOSTIF_TRAP_GROUP_ATTR_QUEUE")[-1]
            
        self.dump_dict[SONiC.ASIC_TRAP_GROUP_OBJ + ":" + self.SAI_GROUP] =  temp_dump 
            
    def __fetch_policier_sai(self, ctx):
        
        tbl = swsscommon.Table(ctx.asic_db, SONiC.ASIC_POLICER_OBJ)
        temp_dump = {"Policer SAI Object" : "Not Found"}
        
        if self.SAI_POLICER is not None:
            temp_dump = dict(tbl.get(self.SAI_POLICER)[1])
            
        self.dump_dict[SONiC.ASIC_POLICER_OBJ + ":" + self.SAI_POLICER] =  temp_dump
           
    
    def __fetch_queue_sai(self, ctx):
        
        tbl = swsscommon.Table(ctx.asic_db, SONiC.ASIC_QUEUE_OBJ)
        temp_dump = {"Queue SAI Object" : "Not Found"}
                
        if self.SAI_QUEUE is not None:
            for key in tbl.getKeys():
                q_index = tbl.hget(key, "SAI_QUEUE_ATTR_INDEX")[-1]
                if q_index == self.SAI_QUEUE:
                    temp_dump = dict(tbl.get(key)[1])
                    break
             
        self.dump_dict[SONiC.ASIC_QUEUE_OBJ + ":" + self.SAI_QUEUE] =  temp_dump
    
     
    
    
    
    
               
            



