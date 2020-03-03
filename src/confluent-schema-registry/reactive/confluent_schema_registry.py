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

import os, sys, stat
import socket
import json
import base64
import tempfile
from OpenSSL import crypto
from subprocess import check_call
from pathlib import Path
from charmhelpers.core import (hookenv, unitdata, host)

from charms.reactive import (when, when_not, hook, when_file_changed,
                             remove_state, set_state, endpoint_from_flag,
                             set_flag)
from charms.layer import tls_client
from charms.reactive.helpers import data_changed
from charmhelpers.core.hookenv import log
from charms.layer.confluent_schema_registry import (keystore_password,
                             ca_crt_path, server_crt_path, server_key_path,
                             client_crt_path, client_key_path,
                             confluent_schema_registry, SCHEMA_REG_PORT,
                             SCHEMA_REG_DATA)


@when('apt.installed.confluent-schema-registry')
@when_not('zookeeper.joined')
def waiting_for_zookeeper():
    hookenv.status_set('blocked', 'waiting for relation to zookeeper')


@when('apt.installed.confluent-schema-registry', 'zookeeper.joined')
@when_not('confluent_schema_registry.started', 'zookeeper.ready')
def waiting_for_zookeeper_ready(zk):
    schemareg = confluent_schema_registry()
    schemareg.install()
    schemareg.daemon_reload()
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
    config = hookenv.config()
    if config['ssl_cert']:
        set_state('certificates.available')
    else:
        hookenv.status_set('waiting', 'Waiting relation to certificate authority.')


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
    if confluentregistry.is_running():
        confluentregistry.stop()
    zks = zk.zookeepers()
    confluentregistry.install(zk_units=zks)
    if not confluentregistry.is_running():
        confluentregistry.start()
    hookenv.open_port(SCHEMA_REG_PORT)
    # set app version string for juju status output
    confluentregistryversion = confluentregistry.version()
    hookenv.application_version_set(confluentregistryversion)
    hookenv.status_set('active', 'ready')
    set_state('confluent_schema_registry.started')


@when('config.changed', 'zookeeper.ready')
def config_changed(zk):
    for k, v in hookenv.config().items():
        if k.startswith('nagios') and data_changed('confluent_schema_registry.config.{}'.format(k),
                                                   v):
            # Trigger a reconfig of nagios if relation established
            remove_state('confluent_schema_registry.nrpe_helper.registered')
    # Something must have changed if this hook fired, trigger reconfig
    remove_state('config.changed')
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
    if kafkareg.is_running():
        kafkareg.stop()
    kafkareg.install(zk_units=zks)
    if not kafkareg.is_running():
        kafkareg.start()
    hookenv.status_set('active', 'ready')


@when('config.changed.ssl_key_password', 'confluent_schema_registry.started')
def change_ssl_key():
    config = hookenv.config()
    password = keystore_password()
    new_password = config['ssl_key_password']
    for jks_type in ('server', 'client', 'server.truststore'):
        jks_path = os.path.join(
            SCHEMA_REG_DATA,
            "confluent_schema_registry.{}.jks".format(jks_type)
        )
        log('modifying password')
        # import the pkcs12 into the keystore
        check_call([
            'keytool',
            '-v', '-storepasswd',
            '-new', new_password,
            '-storepass', password,
            '-keystore', jks_path
        ])
    path = os.path.join(
        SCHEMA_REG_DATA,
        'keystore.secret'
    )
    if os.path.isfile(path):
        os.remove(path)
        with os.fdopen(
                os.open(path, os.O_WRONLY | os.O_CREAT, 0o440),
                'wb') as f:
            if config['ssl_key_password']:
                token = config['ssl_key_password'].encode("utf-8")
                f.write(token)
    import_srv_crt_to_keystore()
    import_ca_crt_to_keystore()
    remove_state('config.changed.ssl_key_password')


@when('tls_client.certs.changed')
def import_srv_crt_to_keystore():
    config = hookenv.config()
    for cert_type in ('server', 'client'):
        password = keystore_password()
        crt_path = os.path.join(
            SCHEMA_REG_DATA,
            "{}.crt".format(cert_type)
        )
        key_path = os.path.join(
            SCHEMA_REG_DATA,
            "{}.key".format(cert_type)
        )

        if os.path.isfile(crt_path) and os.path.isfile(key_path):
            with open(crt_path, 'rt') as f:
                cert = f.read()
                loaded_cert = crypto.load_certificate(
                    crypto.FILETYPE_PEM,
                    cert
                )
                if not data_changed(
                    'confluent_schema_registry{}_certificate'.format(cert_type),
                    cert
                ):
                    if not config['ssl_key_password']:
                        log('server certificate of key file missing')
                        return

            with open(key_path, 'rt') as f:
                loaded_key = crypto.load_privatekey(
                    crypto.FILETYPE_PEM,
                    f.read()
                )

            with tempfile.NamedTemporaryFile() as tmp:
                log('server certificate changed')

                keystore_path = os.path.join(
                    SCHEMA_REG_DATA,
                    "confluent_schema_registry.{}.jks".format(cert_type)
                )
                if os.path.isfile(keystore_path):
                    os.remove(keystore_path)
                pkcs12 = crypto.PKCS12Type()
                pkcs12.set_certificate(loaded_cert)
                pkcs12.set_privatekey(loaded_key)
                pkcs12_data = pkcs12.export(password)
                log('opening tmp file {}'.format(tmp.name))

                # write cert and private key to the pkcs12 file
                tmp.write(pkcs12_data)
                tmp.flush()

                log('importing pkcs12')
                # import the pkcs12 into the keystore
                check_call([
                    'keytool',
                    '-v', '-importkeystore',
                    '-srckeystore', str(tmp.name),
                    '-srcstorepass', password,
                    '-srcstoretype', 'PKCS12',
                    '-destkeystore', keystore_path,
                    '-deststoretype', 'JKS',
                    '-deststorepass', password,
                    '--noprompt'
                ])
                set_state('confluent_schema_registry.{}.keystore.saved'.format(cert_type))

        remove_state('confluent_schema_registry.started')
        remove_state('tls_client.certs.changed')


@when('tls_client.ca_installed')
@when_not('confluent_schema_registry.ca.keystore.saved')
def import_ca_crt_to_keystore():
    config = hookenv.config()
    if os.path.isfile(ca_crt_path):
        with open(ca_crt_path, 'rt') as f:
            changed = data_changed('ca_certificate', f.read())
            if not changed:
                if config['ssl_key_password']:
                    changed = 'true'

        if changed:
            ca_keystore = os.path.join(
                SCHEMA_REG_DATA,
                "confluent_schema_registry.server.truststore.jks"
            )
            if os.path.isfile(ca_keystore):
                os.remove(ca_keystore)
            check_call([
                'keytool',
                '-import', '-trustcacerts', '-noprompt',
                '-keystore', ca_keystore,
                '-storepass', keystore_password(),
                '-file', ca_crt_path
            ])

            remove_state('tls_client.ca_installed')
            set_state('confluent_schema_registry.ca.keystore.saved')


@when('certificates.ca.changed')
def install_root_ca_cert():
    cert_provider = endpoint_from_flag('certificates.ca.available')
    host.install_ca_cert(cert_provider.root_ca_cert)
    set_state('cert_provider.ca.changed')


@when('certificates.available', 'apt.installed.confluent-schema-registry')
def send_data():
    '''Send the data that is required to create a server certificate for
    this server.'''
    config = hookenv.config()
    if config['ssl_cert']:
        for certs_path in ('server_crt_path', 'client_crt_path'):
            with open(certs_path, "wb") as fs:
                os.chmod(certs_path, stat.S_IRUSR |
                         stat.S_IWUSR |
                         stat.S_IRGRP |
                         stat.S_IROTH)
                fs.write(base64.b64decode(config['ssl_cert']))
            if config['ssl_key']:
                with open(certs_path, "wb") as fks:
                    os.chmod(certs_path, stat.S_IWUSR |
                              stat.S_IWUSR )
                    fks.write(base64.b64decode(config['ssl_key']))
        import_srv_crt_to_keystore()
        if config['ssl_ca']:
            with open(ca_crt_path, "wb") as fca:
                os.chmod(ca_crt_path, stat.S_IRUSR |
                         stat.S_IWUSR |
                         stat.S_IRGRP |
                         stat.S_IROTH)
                fca.write(base64.b64decode(config['ssl_ca']))
        import_ca_crt_to_keystore()
    else:
        common_name = hookenv.unit_private_ip()
        common_public_name = hookenv.unit_public_ip()
        sans = [
            common_name,
            common_public_name,
            hookenv.unit_public_ip(),
            socket.gethostname(),
            socket.getfqdn(),
        ]

        # maybe they have extra names they want as SANs
        extra_sans = hookenv.config('subject_alt_names')
        if extra_sans and not extra_sans == "":
            sans.extend(extra_sans.split())

        # Request a server cert with this information.
        tls_client.request_server_cert(common_name, sans,
                                   crt_path=server_crt_path,
                                   key_path=server_key_path)

        # Request a client cert with this information.
        tls_client.request_client_cert(common_name, sans,
                                   crt_path=client_crt_path,
                                   key_path=client_key_path)


@when('confluent_schema_registry.started')
@when_not('zookeeper.ready')
def stop_kafka_waiting_for_zookeeper_ready():
    hookenv.status_set('maintenance', 'zookeeper not ready, stopping confluent_schema_registry')
    confluent_schema_registry = confluent_schema_registry()
    hookenv.close_port(SCHEMA_REG_PORT)
    confluent_schema_registry.stop()
    remove_state('confluent_schema_registry.started')
    hookenv.status_set('waiting', 'waiting for zookeeper to become ready')
