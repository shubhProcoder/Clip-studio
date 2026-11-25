"""
Publishing with the user's own credentials: YouTube Shorts (YouTube Data API v3, installed-app OAuth),
Instagram Reels (Instagram API with Facebook Login) and a TikTok publish pack (files to post by hand).
Standard-library HTTP behind an injectable transport (http.Transport), so tests never touch the network.
Tokens live in paths.CONFIG_DIR only and are redacted from every message (store.redact).
"""

PLATFORMS = ("youtube", "instagram", "pack")
