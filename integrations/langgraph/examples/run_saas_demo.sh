#!/bin/bash

# SaaS Entitlements Demo Runner
# This script loads the demo data and runs the multi-agent demo

set -e  # Exit on error

echo "================================"
echo "SaaS Entitlements Copilot Demo"
echo "================================"
echo ""

# Check if virtual environment is activated
if [[ -z "$VIRTUAL_ENV" ]]; then
    echo "Warning: Virtual environment not activated"
    echo "Activating .venv..."
    source ../.venv/bin/activate
fi

# Check for API keys
if [[ -z "$OPENAI_API_KEY" ]]; then
    echo "Error: OPENAI_API_KEY not set"
    echo "Please set it in your .env file"
    exit 1
fi

# Step 1: Load data
echo "Step 1: Loading demo data into Cognee..."
echo "This may take a few minutes..."
echo ""
cd data/cognee_mock_saas_entitlements_demo
python load_into_cognee.py

if [ $? -ne 0 ]; then
    echo "Error: Failed to load data"
    exit 1
fi

echo ""
echo "OK: Data loaded successfully!"
echo ""

# Step 2: Run agents demo
echo "Step 2: Running multi-agent demo..."
echo ""
cd ../..
python saas_entitlements_agents.py

if [ $? -ne 0 ]; then
    echo "Error: Demo failed"
    exit 1
fi

echo ""
echo "================================"
echo "OK: Demo completed successfully!"
echo "================================"
