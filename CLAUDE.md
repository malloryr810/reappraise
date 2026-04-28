# Item Appraisal Tool — ReStore Pricing Assistant

## What this project does
User photographs a donated item → Google Vision identifies it → eBay lookup finds market median → applies condition multiplier to estimate fair resale price for a Habitat for Humanity ReStore.

## Motivation
Built to automate the manual price lookup process used at a Habitat for Humanity ReStore. Staff would look up donated items and price them at roughly 60% of market value. This tool does that automatically from a photo.

## Pricing logic
Condition multiplier replaces the flat rate (not stacked on top):
- terrible: 0.4 / poor: 0.5 / fair: 0.6 / good: 0.7 / like_new: 0.8
- "fair" is the default and matches the ReStore's standard pricing policy

## Project structure
- vision.py — Google Vision REST API, plain API key, LABEL_DETECTION + OBJECT_LOCALIZATION
- ebay.py — eBay Browse API + mock fallback + condition pricing
- app.py — FastAPI, POST /appraise, condition as optional query param

## Environment variables needed
- GOOGLE_VISION_API_KEY — plain API key from GCP console
- EBAY_CLIENT_ID and EBAY_CLIENT_SECRET — pending developer account approval

## Current status
- 23/23 tests passing
- Google Vision working with real images
- Mock eBay active, real credentials pending (1-2 days)
- No frontend yet

## Next task when resuming
Swap mock eBay for real eBay Browse API once credentials are approved. Then update README.
