# -*- coding: utf-8 -*-

import requests
import time
import json

def send_discord_notification(webhook_url, message):
    """
    Sends a message to the specified Discord webhook URL with rate limit handling.

    :param webhook_url: The Discord webhook URL.
    :param message: The message content to send.
    :return: Boolean indicating success or failure.
    """
    if not webhook_url:
        print("❌ Discord webhook URL is not provided.")
        return False

    # Proactively sleep for 2 seconds to avoid hitting rate limits.
    # Discord's general webhook limit is 30 messages per 60 seconds.
    time.sleep(2)

    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                webhook_url,
                json={"content": message},
                timeout=10  # Increased timeout for better reliability
            )

            # Success (200 OK or 204 No Content)
            if resp.status_code in [200, 204]:
                print("✅ Discord notification sent successfully.")
                return True

            # Rate limit error handling
            elif resp.status_code == 429:
                wait_time = 2  # Default wait time
                try:
                    # Discord provides wait time in the JSON body
                    wait_time = resp.json().get("retry_after", 2)
                except json.JSONDecodeError:
                    # Fallback to header if body is not JSON
                    try:
                        wait_time = int(float(resp.headers.get("Retry-After")))
                    except (ValueError, TypeError):
                        # If all else fails, use exponential backoff
                        wait_time = 5 * (attempt + 1)
                
                # Add a small buffer to be safe
                wait_time += 0.5

                print(f"⏱️ Rate limited (429). Retrying after {wait_time:.1f} seconds... (Attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue  # Move to the next attempt

            # Other client/server errors (e.g., 404 Not Found, 400 Bad Request)
            else:
                print(f"⚠️ Discord API Error: {resp.status_code} / Body: {resp.text}")
                # Do not retry on these errors as they are likely permanent
                return False

        except requests.exceptions.RequestException as e:
            print(f"❌ Failed to send Discord notification due to a network error: {e}")
            # Retry on network errors with exponential backoff
            wait_time = 5 * (attempt + 1)
            print(f"Retrying in {wait_time} seconds...")
            time.sleep(wait_time)

    print(f"❌ Failed to send Discord notification after {max_retries} attempts.")
    return False
