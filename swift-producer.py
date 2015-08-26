#!/usr/bin/env python

import json
import logging
import os
import random

from multiprocessing.pool import ThreadPool

import requests

from influxdb import *

from keystoneclient.v2_0 import client
from swift.common.ring import Ring

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

keystone_config = {
	"username" : os.getenv("OS_USERNAME"),
	"password" : os.getenv("OS_PASSWORD"),
	"tenant_name" : os.getenv("OS_TENANT_NAME"),
	"auth_url" : os.getenv("OS_AUTH_URL")
}
keystone = client.Client(**keystone_config)

ring = Ring("/etc/swift/account.ring.gz")

def fetch(tenant):
	account = "AUTH_%s" % tenant
	partition = ring.get_part(account, None, None)
	nodes = ring.get_part_nodes(partition)
	random.shuffle(nodes)
	for node in nodes:
		url = "http://%s:%s/%s/%s/%s" % (node["ip"], node["port"], node["device"], partition, account)
		try:
			response = requests.head(url, timeout=5)
			if response.status_code == 204:
				return {
					"measurement": "project-usage.object",
					"fields": {
						"projectid" : tenant,
						"containers" : int(response.headers["x-account-container-count"]),
						"objects" : int(response.headers["x-account-object-count"]),
						"bytes" : int(response.headers["x-account-bytes-used"]),
						"quota" : int(response.headers["x-account-meta-quota-bytes"]) if "x-account-meta-quota-bytes" in response.headers else None
					},
					"tags": {
						"projectid": tenant
					}
				}
			elif response.status_code == 404:
				return None
			else:
				logging.warning("error fetching %s [HTTP %s]", url, response.status_code)
		except:
			logging.warning("error fetching %s", url, exc_info=True)
	logging.error("failed to fetch info for tenant %s", tenant)
	return None

tenants = [tenant.id for tenant in keystone.tenants.list()]
random.shuffle(tenants)

report = {}
for tenant, stats in zip(tenants, ThreadPool().map(fetch, tenants)):
	report[tenant] = stats

idb = InfluxDBClient(host='130.56.248.73', database='reporting')

idb.write_points(points=report, batch_size=1000) 	

