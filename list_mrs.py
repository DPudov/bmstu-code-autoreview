import requests
from dotenv import load_dotenv
import os

load_dotenv('script.conf')

# your GitLab instance URL and personal access token
GITLAB_URL = os.environ.get("GITLAB_URL", "https://git.iu7.bmstu.ru").rstrip("/")
PRIVATE_TOKEN = os.environ.get("GITLAB_TOKEN")

# user ID of the assignee you want to filter by
ASSIGNEE_ID = os.environ.get("ASSIGNEE_ID")

# API endpoint for merge requests, filtered by assignee_id and state (e.g., 'opened')
api_endpoint = f"{GITLAB_URL}/api/v4/merge_requests"
params = {
    "assignee_id": ASSIGNEE_ID,
    "state": "opened",  # or "all", "merged", "closed"
    "per_page": 100, # Adjust as needed, max 100 per page
}
headers = {
    "Private-Token": PRIVATE_TOKEN
}

try:
    response = requests.get(api_endpoint, params=params, headers=headers)
    response.raise_for_status()  # Raise an exception for bad status codes

    assigned_merge_requests = response.json()

    if assigned_merge_requests:
        print(f"Assigned Merge Requests for User ID {ASSIGNEE_ID}:")
        for mr in assigned_merge_requests:
            print(f"  - Title: {mr['title']}")
            print(f"    Web URL: {mr['web_url']}")
            print(f"    Project ID: {mr['project_id']}")
            print(f"    Assignee: {mr['assignee']['name']}")
            print("-" * 30)
    else:
        print(f"No assigned merge requests found for User ID {ASSIGNEE_ID}.")

except requests.exceptions.RequestException as e:
    print(f"Error making API request: {e}")
except Exception as e:
        print(f"An unexpected error occurred: {e}")