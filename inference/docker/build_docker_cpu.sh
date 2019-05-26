#!/bin/bash
SCRIPTPATH="$( cd "$(dirname "$0")" ; pwd -P )"
docker build -t seunglab/seamless:$1 -f Dockerfile.cpu ../../
docker push seunglab/seamless:$1
