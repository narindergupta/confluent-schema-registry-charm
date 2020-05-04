#!/bin/bash

set -eu

export PATH=/usr/lib/jvm/default-java/bin:$PATH

if [ -e "/etc/schema-registry/broker.env" ]; then
    . /etc/schema-registry/broker.env
fi

/usr/bin/schema-registry-start /etc/schema-registry/schema-registry.properties
