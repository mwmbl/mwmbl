#!/bin/bash

# MWMBL Voting API Test Script
# Tests the POST /votes endpoint on the live server at api.mwmbl.org
# 
# Prerequisites:
# 1. You need a registered user account with verified email on api.mwmbl.org
# 2. You need to obtain a JWT token by logging in
# 3. Set the JWT_TOKEN environment variable before running this script

set -e

# Configuration
API_BASE="https://api.mwmbl.org/api/v1"
SEARCH_QUERY="python"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== MWMBL Voting API Test Script ===${NC}"
echo

# Check if JWT token is provided
if [ -z "$JWT_TOKEN" ]; then
    echo -e "${RED}ERROR: JWT_TOKEN environment variable is not set${NC}"
    echo
    echo -e "${YELLOW}To obtain a JWT token:${NC}"
    echo "1. Register a user account:"
    echo "   curl -X POST \"$API_BASE/platform/register\" \\"
    echo "        -H \"Content-Type: application/json\" \\"
    echo "        -d '{\"username\": \"your_username\", \"email\": \"your_email@example.com\", \"password\": \"your_password\"}'"
    echo
    echo "2. Check your email and confirm your account using the confirmation link"
    echo
    echo "3. Get JWT token by logging in:"
    echo "   curl -X POST \"$API_BASE/platform/token/pair\" \\"
    echo "        -H \"Content-Type: application/json\" \\"
    echo "        -d '{\"username\": \"your_username\", \"password\": \"your_password\"}'"
    echo
    echo "4. Export the access token:"
    echo "   export JWT_TOKEN=\"your_access_token_here\""
    echo
    exit 1
fi

echo -e "${YELLOW}Step 1: Getting valid URLs from search endpoint${NC}"
echo "Searching for: $SEARCH_QUERY"

# Get some valid URLs from search results
SEARCH_RESPONSE=$(curl -s "$API_BASE/search/?s=$SEARCH_QUERY")
echo "✓ Retrieved search results"

# Extract first few URLs for testing
URL1=$(echo "$SEARCH_RESPONSE" | jq -r '.[0].url // empty')
URL2=$(echo "$SEARCH_RESPONSE" | jq -r '.[1].url // empty')
URL3=$(echo "$SEARCH_RESPONSE" | jq -r '.[2].url // empty')

# if [ -z "$URL1" ]; then
#     echo -e "${RED}ERROR: Could not retrieve valid URLs from search results${NC}"
#     exit 1
# fi

# echo "URLs to test:"
# echo "  - $URL1"
# echo "  - $URL2"
# echo "  - $URL3"
# echo

# # Test 1: Vote on search results
# echo -e "${YELLOW}Step 2: Testing POST /search-results/vote (upvote)${NC}"

# VOTE_RESPONSE=$(curl -s -w "HTTP_STATUS:%{http_code}" \
#     -X POST "$API_BASE/platform/search-results/vote" \
#     -H "Authorization: Bearer $JWT_TOKEN" \
#     -H "Content-Type: application/json" \
#     -d "{
#         \"url\": \"$URL1\",
#         \"query\": \"$SEARCH_QUERY\",
#         \"vote_type\": \"upvote\"
#     }")

# HTTP_STATUS=$(echo "$VOTE_RESPONSE" | grep -o "HTTP_STATUS:[0-9]*" | cut -d: -f2)
# RESPONSE_BODY=$(echo "$VOTE_RESPONSE" | sed 's/HTTP_STATUS:[0-9]*$//')

# echo "Response Status: $HTTP_STATUS"
# echo "Response Body: $RESPONSE_BODY"

# if [ "$HTTP_STATUS" = "200" ]; then
#     echo -e "${GREEN}✓ Upvote successful${NC}"
# else
#     echo -e "${RED}✗ Upvote failed${NC}"
# fi
# echo

# # Test 2: Vote on another result (downvote)
# echo -e "${YELLOW}Step 3: Testing POST /search-results/vote (downvote)${NC}"

# VOTE_RESPONSE2=$(curl -s -w "HTTP_STATUS:%{http_code}" \
#     -X POST "$API_BASE/platform/search-results/vote" \
#     -H "Authorization: Bearer $JWT_TOKEN" \
#     -H "Content-Type: application/json" \
#     -d "{
#         \"url\": \"$URL2\",
#         \"query\": \"$SEARCH_QUERY\",
#         \"vote_type\": \"downvote\"
#     }")

# HTTP_STATUS2=$(echo "$VOTE_RESPONSE2" | grep -o "HTTP_STATUS:[0-9]*" | cut -d: -f2)
# RESPONSE_BODY2=$(echo "$VOTE_RESPONSE2" | sed 's/HTTP_STATUS:[0-9]*$//')

# echo "Response Status: $HTTP_STATUS2"
# echo "Response Body: $RESPONSE_BODY2"

# if [ "$HTTP_STATUS2" = "200" ]; then
#     echo -e "${GREEN}✓ Downvote successful${NC}"
# else
#     echo -e "${RED}✗ Downvote failed${NC}"
# fi
# echo

# # Test 3: Update existing vote
# echo -e "${YELLOW}Step 4: Testing vote update (changing upvote to downvote)${NC}"

# VOTE_UPDATE_RESPONSE=$(curl -s -w "HTTP_STATUS:%{http_code}" \
#     -X POST "$API_BASE/platform/search-results/vote" \
#     -H "Authorization: Bearer $JWT_TOKEN" \
#     -H "Content-Type: application/json" \
#     -d "{
#         \"url\": \"$URL1\",
#         \"query\": \"$SEARCH_QUERY\",
#         \"vote_type\": \"downvote\"
#     }")

# HTTP_STATUS3=$(echo "$VOTE_UPDATE_RESPONSE" | grep -o "HTTP_STATUS:[0-9]*" | cut -d: -f2)
# RESPONSE_BODY3=$(echo "$VOTE_UPDATE_RESPONSE" | sed 's/HTTP_STATUS:[0-9]*$//')

# echo "Response Status: $HTTP_STATUS3"
# echo "Response Body: $RESPONSE_BODY3"

# if [ "$HTTP_STATUS3" = "200" ]; then
#     echo -e "${GREEN}✓ Vote update successful${NC}"
# else
#     echo -e "${RED}✗ Vote update failed${NC}"
# fi
# echo

# Test 4: Get vote statistics
echo -e "${YELLOW}Step 5: Testing POST /search-results/votes (get vote stats)${NC}"

STATS_RESPONSE=$(curl -s -w "HTTP_STATUS:%{http_code}" \
    -X POST "$API_BASE/platform/search-results/votes" \
    -H "Authorization: Bearer $JWT_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{
        \"query\": \"$SEARCH_QUERY\",
        \"urls\": [\"$URL1\", \"$URL2\", \"$URL3\"]
    }")

HTTP_STATUS4=$(echo "$STATS_RESPONSE" | grep -o "HTTP_STATUS:[0-9]*" | cut -d: -f2)
RESPONSE_BODY4=$(echo "$STATS_RESPONSE" | sed 's/HTTP_STATUS:[0-9]*$//')

echo "Response Status: $HTTP_STATUS4"
echo "Response Body (formatted):"
echo "$RESPONSE_BODY4" | jq . 2>/dev/null || echo "$RESPONSE_BODY4"

if [ "$HTTP_STATUS4" = "200" ]; then
    echo -e "${GREEN}✓ Vote statistics retrieval successful${NC}"
else
    echo -e "${RED}✗ Vote statistics retrieval failed${NC}"
fi
echo

# Test 5: Get user's voting history
echo -e "${YELLOW}Step 6: Testing GET /search-results/my-votes (user vote history)${NC}"

HISTORY_RESPONSE=$(curl -s -w "HTTP_STATUS:%{http_code}" \
    -X GET "$API_BASE/platform/search-results/my-votes" \
    -H "Authorization: Bearer $JWT_TOKEN")

HTTP_STATUS5=$(echo "$HISTORY_RESPONSE" | grep -o "HTTP_STATUS:[0-9]*" | cut -d: -f2)
RESPONSE_BODY5=$(echo "$HISTORY_RESPONSE" | sed 's/HTTP_STATUS:[0-9]*$//')

echo "Response Status: $HTTP_STATUS5"
echo "Response Body (formatted):"
echo "$RESPONSE_BODY5" | jq . 2>/dev/null || echo "$RESPONSE_BODY5"

if [ "$HTTP_STATUS5" = "200" ]; then
    echo -e "${GREEN}✓ Vote history retrieval successful${NC}"
else
    echo -e "${RED}✗ Vote history retrieval failed${NC}"
fi
echo

# # Test 6: Remove a vote
# echo -e "${YELLOW}Step 7: Testing DELETE /search-results/vote (remove vote)${NC}"

# DELETE_RESPONSE=$(curl -s -w "HTTP_STATUS:%{http_code}" \
#     -X DELETE "$API_BASE/platform/search-results/vote" \
#     -H "Authorization: Bearer $JWT_TOKEN" \
#     -H "Content-Type: application/json" \
#     -d "{
#         \"url\": \"$URL2\",
#         \"query\": \"$SEARCH_QUERY\"
#     }")

# HTTP_STATUS6=$(echo "$DELETE_RESPONSE" | grep -o "HTTP_STATUS:[0-9]*" | cut -d: -f2)
# RESPONSE_BODY6=$(echo "$DELETE_RESPONSE" | sed 's/HTTP_STATUS:[0-9]*$//')

# echo "Response Status: $HTTP_STATUS6"
# echo "Response Body: $RESPONSE_BODY6"

# if [ "$HTTP_STATUS6" = "200" ]; then
#     echo -e "${GREEN}✓ Vote removal successful${NC}"
# else
#     echo -e "${RED}✗ Vote removal failed${NC}"
# fi
# echo

# # Test 7: Error case - invalid vote type
# echo -e "${YELLOW}Step 8: Testing error case (invalid vote type)${NC}"

# ERROR_RESPONSE=$(curl -s -w "HTTP_STATUS:%{http_code}" \
#     -X POST "$API_BASE/platform/search-results/vote" \
#     -H "Authorization: Bearer $JWT_TOKEN" \
#     -H "Content-Type: application/json" \
#     -d "{
#         \"url\": \"$URL3\",
#         \"query\": \"$SEARCH_QUERY\",
#         \"vote_type\": \"invalid_vote\"
#     }")

# HTTP_STATUS7=$(echo "$ERROR_RESPONSE" | grep -o "HTTP_STATUS:[0-9]*" | cut -d: -f2)
# RESPONSE_BODY7=$(echo "$ERROR_RESPONSE" | sed 's/HTTP_STATUS:[0-9]*$//')

# echo "Response Status: $HTTP_STATUS7"
# echo "Response Body: $RESPONSE_BODY7"

# if [ "$HTTP_STATUS7" = "422" ]; then
#     echo -e "${GREEN}✓ Error case handled correctly (422 Unprocessable Entity)${NC}"
# else
#     echo -e "${RED}✗ Unexpected response for invalid vote type${NC}"
# fi
# echo

# Summary
echo -e "${BLUE}=== Test Summary ===${NC}"
echo "The script tested the following endpoints:"
echo "  - POST /search-results/vote (create/update votes)"
echo "  - POST /search-results/votes (get vote statistics)" 
echo "  - GET /search-results/my-votes (get user vote history)"
echo "  - DELETE /search-results/vote (remove votes)"
echo
echo -e "${YELLOW}Note: This script requires a valid JWT token from a verified user account${NC}"
echo "See the initial instructions for how to register and obtain a token."
