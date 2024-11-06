#!/bin/bash
source /opt/rh/rh-python38/enable
python3.8 -m uvicorn llama:app --host localhost --port 25010 --reload