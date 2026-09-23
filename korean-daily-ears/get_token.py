"""Run once on your PC to get the YouTube secrets for GitHub.

1. Put the OAuth "Desktop app" client file next to this script as client_secret.json
2. pip install google-auth-oauthlib && python get_token.py
3. Log in with the Korean Daily Ears channel and copy the 3 printed lines into GitHub secrets
"""
from google_auth_oauthlib.flow import InstalledAppFlow

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", ["https://www.googleapis.com/auth/youtube"])
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
print("YT_CLIENT_ID =", creds.client_id)
print("YT_CLIENT_SECRET =", creds.client_secret)
print("YT_REFRESH_TOKEN =", creds.refresh_token)
