#!/bin/bash
# Dashboard launcher script

cd "$(dirname "$0")"

# Install dependencies if needed
if ! python3 -c "import flask" 2>/dev/null; then
    echo "Installing Flask..."
    pip3 install -r requirements.txt
fi

# Run dashboard
echo "🐳 Starting Whale Follower Dashboard on http://0.0.0.0:5000"
python3 app.py
