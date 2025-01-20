#!/bin/bash
python3 -m venv venv
source ./venv/bin/activate
python3 -m pip install -r requirements.txt
python3 -m uvicorn llama:app --host localhost --port 25010 --reload