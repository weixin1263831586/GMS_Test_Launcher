#!/bin/bash
# GMS Auto Test Web Application Startup Script

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=========================================="
echo "  GMS Auto Test Web Application"
echo "=========================================="
echo ""

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Check if Python 3 is installed
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: Python 3 is not installed${NC}"
    echo "Please install Python 3 first"
    exit 1
fi

echo -e "${GREEN}✓ Python 3 found${NC}"

# Check if pip is installed
if ! command -v pip3 &> /dev/null; then
    echo -e "${RED}Error: pip3 is not installed${NC}"
    echo "Please install pip3 first"
    exit 1
fi

echo -e "${GREEN}✓ pip3 found${NC}"

# Install dependencies
echo ""
echo "Installing dependencies..."
pip3 install -r requirements.txt

if [ $? -ne 0 ]; then
    echo -e "${RED}Error: Failed to install dependencies${NC}"
    exit 1
fi

echo -e "${GREEN}✓ Dependencies installed${NC}"

# Get IP address
IP_ADDRESS=$(hostname -I | awk '{print $1}')
if [ -z "$IP_ADDRESS" ]; then
    IP_ADDRESS="localhost"
fi

# Start the server
echo ""
echo "=========================================="
echo "Starting server..."
echo "=========================================="
echo ""
echo -e "${GREEN}Server will be available at:${NC}"
echo -e "  Local:   http://localhost:5000"
echo -e "  Network: http://$IP_ADDRESS:5000"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop the server${NC}"
echo ""
echo "=========================================="
echo ""

# Run the application
python3 app.py
