# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from charms.layer.confluent_schema_registry import (confluent_schema_registry,
                                                SCHEMA_REG_PORT)

from charmhelpers.core import hookenv, unitdata

from charms.reactive import (when, when_not, hook, set_flag,
                             remove_state, set_state)
from charms.reactive.helpers import data_changed


@when('apt.installed.confluent-schema-registry')
@when_not('zookeeper.joined')
def waiting_for_zookeeper():
    hookenv.status_set('blocked', 'waiting for relation to zookeeper')


@when('apt.installed.confluent-schema-registry', 'zookeeper.joined')
@when_not('confluent_schema_registry.started', 'zookeeper.ready')
def waiting_for_zookeeper_ready(zk):
    hookenv.status_set('waiting', 'waiting for zookeeper to become ready')


@hook('upgrade-charm')
def upgrade_charm():
    remove_state('confluent_schema_registry.nrpe_helper.installed')
    remove_state('confluent_schema_registry.started')


@when_not(
    'confluent_schema_registry.ca.keystore.saved',
    'confluent_schema_registry.server.keystore.saved'
)
@when('apt.installed.confluent-schema-registry')
def waiting_for_certificates():
    hookenv.status_set('waiting', 'waiting for easyrsa relation')


@when(
    'apt.installed.confluent-schema-registry',
    'zookeeper.ready',
    'confluent_schema_registry.ca.keystore.saved',
    'confluent_schema_registry.server.keystore.saved'
)
@when_not('confluent_schema_registry.started')
def configure_confluent_schema_registry(zk):
    hookenv.status_set('maintenance', 'setting up confluent_schema_registry')
    confluentregistry = confluent_schema_registry()
    zks = zk.zookeepers()
    confluentregistry.install(zk_units=zks)
    hookenv.open_port(SCHEMA_REG_PORT)
    set_state('confluent_schema_registry.started')
    hookenv.status_set('active', 'ready')
    # set app version string for juju status output
    confluentregistryversion = confluentregistry.version()
    hookenv.application_version_set(confluentregistryversion)


@when('config.changed', 'zookeeper.ready')
def config_changed(zk):
    for k, v in hookenv.config().items():
        if k.startswith('nagios') and data_changed('confluent_schema_registry.config.{}'.format(k),
                                                   v):
            # Trigger a reconfig of nagios if relation established
            remove_state('confluent_schema_registry.nrpe_helper.registered')
    # Something must have changed if this hook fired, trigger reconfig
    remove_state('confluent_schema_registry.started')


@when('confluent_schema_registry.started', 'zookeeper.ready')
def configure_confluent_schema_registry_zookeepers(zk):
    """Configure ready zookeepers and restart kafka if needed.
    As zks come and go, server.properties will be updated. When that file
    changes, restart Kafka and set appropriate status messages.
    """
    zks = zk.zookeepers()
    if not((
            data_changed('zookeepers', zks))):
        return

    hookenv.log('Checking Zookeeper configuration')
    hookenv.status_set('maintenance', 'updating zookeeper instances')
    kafkareg = confluent_schema_registry()
    kafkareg.install(zk_units=zks)
    hookenv.status_set('active', 'ready')


@when('confluent_schema_registry.started')
@when_not('zookeeper.ready')
def stop_kafka_waiting_for_zookeeper_ready():
    hookenv.status_set('maintenance', 'zookeeper not ready, stopping confluent_schema_registry')
    confluent_schema_registry = confluent_schema_registry()
    hookenv.close_port(SCHEMA_REG_PORT)
    confluent_schema_registry.stop()
    remove_state('confluent_schema_registry.started')
    hookenv.status_set('waiting', 'waiting for zookeeper to become ready')
