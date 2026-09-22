#!/bin/sh
# Double-clic dans le Finder : ouvre le tableau de bord dans le navigateur.
exec "$(cd "$(dirname "$0")" && pwd)/tableau"
