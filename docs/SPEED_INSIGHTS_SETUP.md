# Speed Insights Documentation Setup

This document explains how to add the Speed Insights documentation to the ContentHub database.

## Overview

The Speed Insights documentation provides a comprehensive guide for developers on how to integrate Vercel Speed Insights into their projects across various frameworks including:

- Next.js (Pages Router and App Router)
- React (Create React App)
- Remix
- SvelteKit
- Vue
- Nuxt
- Astro
- HTML
- Other frameworks

## Adding the Documentation

### Prerequisites

1. Make sure you have the `MONGO_API_KEY` environment variable set in your `.env` file
2. Ensure you have Python 3 and the required dependencies installed

### Installation

1. Install required Python packages:
   ```bash
   pip install -r requirements.txt
   ```

2. Run the seed script:
   ```bash
   python seed_speed_insights.py
   ```

   Or if you made it executable:
   ```bash
   ./seed_speed_insights.py
   ```

### What the Script Does

The `seed_speed_insights.py` script will:

1. **Create a "Speed Insights" category** (if it doesn't already exist)
   - Name: Speed Insights
   - Description: Learn how to use Vercel Speed Insights to monitor and improve your application performance
   - Accent Color: Vercel blue (#0070f3)
   - Access: Free (accessible to all users)

2. **Add the "Getting started with Speed Insights" content**
   - Comprehensive setup guide
   - Framework-specific implementation instructions
   - Deployment instructions
   - Links to additional resources

### Verifying the Installation

After running the seed script:

1. Log into the ContentHub admin panel
2. Navigate to the Speed Insights category
3. You should see the "Getting started with Speed Insights" documentation
4. Users can now access this documentation to learn about implementing Speed Insights

## Content Structure

The documentation is organized as follows:

- **Prerequisites**: Lists requirements before getting started
- **Setup Steps**: Step-by-step instructions for enabling and installing
- **Framework-Specific Implementation**: Detailed code examples for each supported framework
- **Deployment**: Instructions for deploying to Vercel
- **Next Steps**: Links to additional resources

## Updating the Documentation

To update the documentation content:

1. Modify the `content_text` variable in the `create_getting_started_content()` function in `seed_speed_insights.py`
2. Delete the existing content from the database (via admin panel or MongoDB directly)
3. Re-run the seed script

## Notes

- The script is idempotent - it checks if content already exists before creating
- The category is marked as "free" so all users can access it
- The content uses Markdown formatting for better readability
