import os
import tweepy
from dotenv import load_dotenv

load_dotenv()

# Auth
client = tweepy.Client(
    consumer_key=os.getenv("X_API_KEY"),
    consumer_secret=os.getenv("X_API_SECRET"),
    access_token=os.getenv("X_ACCESS_TOKEN"),
    access_token_secret=os.getenv("X_ACCESS_TOKEN_SECRET"),
)

# Test post
response = client.create_tweet(text="🌩️ Bot is online. Convective outlooks incoming. #wxtwitter #severeweather")

if response.data:
    print(f"✅ Tweet posted! ID: {response.data['id']}")
else:
    print("❌ Something went wrong.")
