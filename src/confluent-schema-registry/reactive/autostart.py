from charms.layer.confluent_schema_registry import confluent_schema_registry

from charmhelpers.core import hookenv

from charms.reactive import when


@when('confluent_schema_registry.started', 'zookeeper.ready')
def autostart_service():
    '''
    Attempt to restart the service if it is not running.
    '''
    schemaregistry = confluent_schema_registry()

    if schemaregistry.is_running():
        hookenv.status_set('active', 'ready')
        return

    for i in range(3):
        hookenv.status_set(
            'maintenance',
            'attempting to restart confluent_schema_registry, '
            'attempt: {}'.format(i+1)
        )
        schemaregistry.restart()
        if schemaregistry.is_running():
            hookenv.status_set('active', 'ready')
            return

    hookenv.status_set('blocked',
            'failed to start confluent_schema_registry; check syslog')
