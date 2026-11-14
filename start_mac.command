#!/bin/bash
cd "$(dirname "$0")"
if [ ! -f config.json ]; then
  cp config.example.json config.json
fi
(sleep 1 && open "http://localhost:8989") &
python3 server.py
