#!/bin/sh
# dpkg postrm for the ForgeMesh Client Manager .deb.
# Worker tokens and task logs live under each user's XDG directories and are
# intentionally left alone; only the system-wide virtual environment goes.
set -e

if [ "$1" = "purge" ]; then
    rm -rf /opt/forgemesh-client
fi

exit 0
