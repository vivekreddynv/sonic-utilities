# COPP Related Tables
CFG_COPP_TRAP_TABLE_NAME       =              "COPP_TRAP"
CFG_COPP_GROUP_TABLE_NAME      =              "COPP_GROUP"
APP_COPP_TABLE_NAME            =              "COPP_TABLE"

ASIC_DB_PREFIX                 =              "ASIC_STATE"

ASIC_TRAP_OBJ                  =              ASIC_DB_PREFIX + ":" + "SAI_OBJECT_TYPE_HOSTIF_TRAP"
ASIC_TRAP_GROUP_OBJ            =              ASIC_DB_PREFIX + ":" + "SAI_OBJECT_TYPE_HOSTIF_TRAP_GROUP"
ASIC_POLICER_OBJ               =              ASIC_DB_PREFIX + ":" + "SAI_OBJECT_TYPE_POLICER"
ASIC_QUEUE_OBJ                 =              ASIC_DB_PREFIX + ":" + "SAI_OBJECT_TYPE_QUEUE"


trap_id_map = { "stp" : "SAI_HOSTIF_TRAP_TYPE_STP" ,
    "lacp" : "SAI_HOSTIF_TRAP_TYPE_LACP" ,
    "eapol" : "SAI_HOSTIF_TRAP_TYPE_EAPOL" ,
    "lldp" : "SAI_HOSTIF_TRAP_TYPE_LLDP" ,
    "pvrst" : "SAI_HOSTIF_TRAP_TYPE_PVRST" ,
    "igmp_query" : "SAI_HOSTIF_TRAP_TYPE_IGMP_TYPE_QUERY" ,
    "igmp_leave" : "SAI_HOSTIF_TRAP_TYPE_IGMP_TYPE_LEAVE" ,
    "igmp_v1_report" : "SAI_HOSTIF_TRAP_TYPE_IGMP_TYPE_V1_REPORT" ,
    "igmp_v2_report" : "SAI_HOSTIF_TRAP_TYPE_IGMP_TYPE_V2_REPORT" ,
    "igmp_v3_report" : "SAI_HOSTIF_TRAP_TYPE_IGMP_TYPE_V3_REPORT" ,
    "sample_packet" : "SAI_HOSTIF_TRAP_TYPE_SAMPLEPACKET" ,
    "switch_cust_range" : "SAI_HOSTIF_TRAP_TYPE_SWITCH_CUSTOM_RANGE_BASE" ,
    "arp_req" : "SAI_HOSTIF_TRAP_TYPE_ARP_REQUEST" ,
    "arp_resp" : "SAI_HOSTIF_TRAP_TYPE_ARP_RESPONSE" ,
    "dhcp" : "SAI_HOSTIF_TRAP_TYPE_DHCP" ,
    "ospf" : "SAI_HOSTIF_TRAP_TYPE_OSPF" ,
    "pim" : "SAI_HOSTIF_TRAP_TYPE_PIM" ,
    "vrrp" : "SAI_HOSTIF_TRAP_TYPE_VRRP" ,
    "bgp" : "SAI_HOSTIF_TRAP_TYPE_BGP" ,
    "dhcpv6" : "SAI_HOSTIF_TRAP_TYPE_DHCPV6" ,
    "ospfv6" : "SAI_HOSTIF_TRAP_TYPE_OSPFV6" ,
    "vrrpv6" : "SAI_HOSTIF_TRAP_TYPE_VRRPV6" ,
    "bgpv6" : "SAI_HOSTIF_TRAP_TYPE_BGPV6" ,
    "neigh_discovery" : "SAI_HOSTIF_TRAP_TYPE_IPV6_NEIGHBOR_DISCOVERY" ,
    "mld_v1_v2" : "SAI_HOSTIF_TRAP_TYPE_IPV6_MLD_V1_V2" ,
    "mld_v1_report" : "SAI_HOSTIF_TRAP_TYPE_IPV6_MLD_V1_REPORT" ,
    "mld_v1_done" : "SAI_HOSTIF_TRAP_TYPE_IPV6_MLD_V1_DONE" ,
    "mld_v2_report" : "SAI_HOSTIF_TRAP_TYPE_MLD_V2_REPORT" ,
    "ip2me" : "SAI_HOSTIF_TRAP_TYPE_IP2ME" ,
    "ssh" : "SAI_HOSTIF_TRAP_TYPE_SSH" ,
    "snmp" : "SAI_HOSTIF_TRAP_TYPE_SNMP" ,
    "router_custom_range" : "SAI_HOSTIF_TRAP_TYPE_ROUTER_CUSTOM_RANGE_BASE" ,
    "l3_mtu_error" : "SAI_HOSTIF_TRAP_TYPE_L3_MTU_ERROR" ,
    "ttl_error" : "SAI_HOSTIF_TRAP_TYPE_TTL_ERROR" ,
    "udld" : "SAI_HOSTIF_TRAP_TYPE_UDLD" ,
    "bfd" : "SAI_HOSTIF_TRAP_TYPE_BFD" ,
    "bfdv6" : "SAI_HOSTIF_TRAP_TYPE_BFDV6" ,
    "src_nat_miss" : "SAI_HOSTIF_TRAP_TYPE_SNAT_MISS" ,
    "dest_nat_miss" : "SAI_HOSTIF_TRAP_TYPE_DNAT_MISS" 
    }