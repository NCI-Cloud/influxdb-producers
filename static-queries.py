hypervisors_query="""
select
        hypervisor_id,
        hypervisor_hostname as hostname,
        host_ip as ip_address,
        vcpus as cpus,
        memory_mb as memory,
        local_gb as local_storage
from
        nova.compute_nodes;
"""
projects_query="""
select
        distinct p.id as project_id,
        p.name as display_name,
        p.enabled as enabled,
        i.hard_limit as instances,
        c.hard_limit as cores,
        r.hard_limit as ram,
        g.total_limit as gigabytes,
        v.total_limit as volumes,
        s.total_limit as snapshots
from
        keystone.project as p left outer join
        (
        select  *  from  nova.quotas
        where deleted = 0 and resource = 'ram'
        ) as r on p.id = r.project_id left outer join
        (
        select  *  from  nova.quotas
        where deleted = 0 and resource = 'instances'
        ) as i on p.id = i.project_id left outer join
        (
        select  *  from  nova.quotas
        where deleted = 0 and resource = 'cores'
        ) as c on p.id = c.project_id left outer join
        (
        select
                project_id,
                sum(if(hard_limit>=0,hard_limit,0)) as total_limit
        from
                cinder.quotas
        where deleted = 0 and resource like 'gigabytes%'
        group by project_id
        ) as g on p.id = g.project_id left outer join
        (
        select
                project_id,
                sum(if(hard_limit>=0,hard_limit,0)) as total_limit
        from
                cinder.quotas
        where deleted = 0 and resource like 'volumes%'
        group by project_id
        ) as v on p.id = v.project_id left outer join
        (
        select
                project_id,
                sum(if(hard_limit>=0,hard_limit,0)) as total_limit
        from
                cinder.quotas
        where deleted = 0 and resource like 'snapshots%'
        group by project_id
        ) as s on p.id = s.project_id;
"""
# note that this is a real pain, since "flavourid" is not unique, but it's the
# term that's used elsewhere
flavours_query = """
select
        id as _flavour_id,
        flavorid as flavour_id,
        name,
        vcpus,
        memory_mb as memory,
        root_gb as root,
        ephemeral_gb as ephemeral,
        is_public as public
from
        nova.instance_types;
"""
instances_query = """
select
        project_id,
        instance_id,
        display_name as name,
        vcpus,
        memory_mb as memory,
        root_gb as root,
        ephemeral_gb as ephemeral,
        instance_type_id as flavour,
        created_at as created,
        deleted_at as deleted,
        unix_timestamp(ifnull(deleted_at,now()))-unix_timestamp(created_at) as allocation_time,
        0 as wall_time,
        0 as cpu_time,
        if(deleted<>0,false,true) as active,
	availability_zone
from
        nova.instances;
"""
volumes_query = """
select
        id as volume_id,
        project_id,
        display_name,
	size,
        created_at as created,
        deleted_at as deleted,
        if(attach_status='attached',true,false) as attached,
        instance_uuid,
	availability_zone
from
        cinder.volumes;
"""
images_query = """
select
        id as image_id,
        owner as project_id,
        name,
        size,
        status,
        is_public as public,
        created_at as created,
        deleted_at as deleted
from
        glance.images;
"""

# This data structure specifes the series name, the query to fill it out, and a list of tags and the source fields in the database results
#
# For each series, we run the query against the OS database, and run the following influx command (logically - obviously, using the api):
# insert <series>,<tag1>=<value1>,... column1=col1_value,column2=col2_value,...
#
# Unless we're explicitly backfilling we'll leave the timestamp off and let the server handle that.
#
# More generally this needs to be modified so that the 'query' field is a function to be called, which returns a dictionary containing
# the data columns. This structure would then be used to turn that output into
# a data point in the series, with tags pulled out automatically.
#
# I'm not sure this would work properly as-is, though, because we don't really
# want to be putting all the tag values into the fields as well - maybe we need
# to treat the tags separately . . . it works fine with this static data,
# because they contain /all/ the info (mostly), but for more derived series it
# makes less sense.
#
# For the initial implementation this will do . . .
static_series = {
	'hypervisors': {
		'query': hypervisors_query,
		'tags': {
			'node': ""
		}
	},
	'projects': {
		'query': projects_query,
		'tags': {
			'project_id': 'project_id'
		}
	},
	'instances': {
		'query': instances_query,
		'tags': {
			'instance_id': 'instance_id',
			'project_id': 'project_id',
			'node': 'availability_zone',
			'flavour_id': 'flavourid',
			'image_id': 'imageid',
			'hypervisor_id': 'hypervisor_id',
			'user_id': 'user_id'
		},
	},
	'flavours': {
		'query': flavours_query,
		'tags': {
			'flavour_id': 'flavour_id'
		}
	},
	'images': {
		'query': images_query,
		'tags': {
			'image_id': 'image_id',
			'project_id': 'project_id'
		}
	},
	'volumes': {
		'query': volumes_query,
		'tags': {
			'volume_id': 'volume_id',
			'project_id': 'project_id',
			'node': 'availability_zone'
		}
	}
}
