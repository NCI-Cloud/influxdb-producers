#!/usr/bin/env python

import json
import pprint
from influxdb import *
import MySQLdb
from MySQLdb import cursors

hypervisors_query="""
select
	id as hypervisor_id,
	hypervisor_hostname as hostname,
	host_ip as ip_address,
	vcpus as cpus,
	memory_mb as memory,
	local_gb as local_storage,
	"NCI" as node
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
	select	*  from  nova.quotas
	where deleted = 0 and resource = 'ram'
	) as r on p.id = r.project_id left outer join
	(
	select	*  from  nova.quotas
	where deleted = 0 and resource = 'instances'
	) as i on p.id = i.project_id left outer join
	(
	select	*  from  nova.quotas
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
	uuid as instance_id,
	display_name as name,
	vcpus,
	memory_mb as memory,
	root_gb as root,
	ephemeral_gb as ephemeral,
	instance_type_id as flavour,
	unix_timestamp(created_at) as created,
	unix_timestamp(deleted_at) as deleted,
	unix_timestamp(ifnull(deleted_at,now()))-unix_timestamp(created_at) as allocation_time,
	0 as wall_time,
	0 as cpu_time,
	if(deleted<>0,false,true) as active,
	host as hypervisor,
	ifnull(availability_zone, '') as availability_zone
from
	nova.instances;
"""
volumes_query = """
select
	id as volume_id,
	project_id,
	display_name,
	size,
	unix_timestamp(created_at) as created,
	unix_timestamp(deleted_at) as deleted,
	if(attach_status='attached',true,false) as attached,
	instance_uuid,
	ifnull(availability_zone, '') as availability_zone
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
	unix_timestamp(created_at) as created,
	unix_timestamp(deleted_at) as deleted
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
		'database': 'nova',
		'query': hypervisors_query,
		'tags': {
			'node': ""
		}
	},
	'projects': {
		'database': 'keystone',
		'query': projects_query,
		'tags': {
			'project_id': 'project_id'
		}
	},
	'instances': {
		'database': 'nova',
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
		'database': 'nova',
		'query': flavours_query,
		'tags': {
			'flavour_id': 'flavour_id'
		}
	},
	'images': {
		'database': 'glance',
		'query': images_query,
		'tags': {
			'image_id': 'image_id',
			'project_id': 'project_id'
		}
	},
	'volumes': {
		'database': 'cinder',
		'query': volumes_query,
		'tags': {
			'volume_id': 'volume_id',
			'project_id': 'project_id',
			'node': 'availability_zone'
		}
	}
}

# We have data sources which we query periodically, creating data points
# accodring to the series definitions here.

def database_query(db, query):
	"""
	Run a query against the database and return the result set
	"""
	cursor = db.cursor(cursors.DictCursor)
	cursor.execute(query)
	rows = cursor.fetchall()
	return rows

def assemble_data_point(measurement, row, time=None):
	"""
	Take a result row from a DB query and assemble them into a data set for influxdb
	"""
	# The data is in the form of dict containing a measurement field, a tags dict,
	# a fields dict, and an optional time field. When the time isn't specified the
	# data point will get the current time as seen by the server.
	
	tmp = { 'measurement': measurement,
		'tags': {},
		'fields': {}}
	
	# we use the relevant entry in the static_series dict to decide how to assemble
	# this
	tags = {}
	if measurement in static_series:
		tags = static_series[measurement]['tags']
	for k in row.keys():
		if k in tags:
			tmp['tags'][k] = row[k]
		tmp['fields'][k] = row[k]
	if time:
		tmp['time'] = time
	return tmp

def assemble_data_points(measurement, rows, time=None):
	tmp = []
	for r in rows:
		tmp.append(assemble_data_point(measurement, r, time))
	return tmp

for k in static_series.keys():
	for j in static_series[k].keys():
		print k, j


db = MySQLdb.connect(host='localhost', user='reporting-update', passwd='needs to be set', db='reporting')

idb = InfluxDBClient(host='130.56.248.73', database='reporting')

for k in static_series.keys():
	data = database_query(db, static_series[k]['query'])
	print k, len(data)
	points = assemble_data_points(k, data)
	print len(points)

	print "Writing %d points to database" % (len(points))
	idb.write_points(points=points, batch_size=1000)
