#!/usr/bin/env python3
"""
Seed script to add Speed Insights documentation content to the ContentHub database.
This script creates a category for Speed Insights and adds the getting started guide.
"""

from pymongo import MongoClient
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

# MongoDB Connection
MONGO_URI = os.environ.get('MONGO_API_KEY')
if not MONGO_URI:
    print("Error: MONGO_API_KEY environment variable not set")
    exit(1)

client = MongoClient(MONGO_URI)
db = client['contenthub']

# Collections
categories_collection = db['categories']
content_collection = db['content']
folders_collection = db['folders']

def create_speed_insights_category():
    """Create or update Speed Insights category"""
    category_name = "Speed Insights"
    
    existing_category = categories_collection.find_one({'name': category_name})
    
    if existing_category:
        print(f"Category '{category_name}' already exists with ID: {existing_category['_id']}")
        return existing_category['_id']
    
    category_data = {
        'name': category_name,
        'description': 'Learn how to use Vercel Speed Insights to monitor and improve your application performance',
        'accent_color': '#0070f3',  # Vercel blue
        'is_free': True,  # Make it accessible to all users
        'created_at': datetime.utcnow()
    }
    
    result = categories_collection.insert_one(category_data)
    print(f"Created category '{category_name}' with ID: {result.inserted_id}")
    return result.inserted_id

def create_getting_started_content(category_id):
    """Create the Getting Started guide content"""
    
    # Check if content already exists
    existing_content = content_collection.find_one({
        'category_id': str(category_id),
        'title': 'Getting started with Speed Insights'
    })
    
    if existing_content:
        print(f"Content 'Getting started with Speed Insights' already exists")
        return existing_content['_id']
    
    content_text = """# Getting started with Speed Insights

This guide will help you get started with using Vercel Speed Insights on your project, showing you how to enable it, add the package to your project, deploy your app to Vercel, and view your data in the dashboard.

To view instructions on using the Vercel Speed Insights in your project for your framework, use the **Choose a framework** dropdown on the right (at the bottom in mobile view).

## Prerequisites

- A Vercel account. If you don't have one, you can [sign up for free](https://vercel.com/signup).
- A Vercel project. If you don't have one, you can [create a new project](https://vercel.com/new).
- The Vercel CLI installed. If you don't have it, you can install it using the following command:

### Installation commands

**pnpm:**
```bash
pnpm i vercel
```

**yarn:**
```bash
yarn i vercel
```

**npm:**
```bash
npm i vercel
```

**bun:**
```bash
bun i vercel
```

## Setup Steps

### 1. Enable Speed Insights in Vercel

On the [Vercel dashboard](/dashboard), select your Project followed by the **Speed Insights** tab. You can also select the button below to be taken there. Then, select **Enable** from the dialog.

> **💡 Note:** Enabling Speed Insights will add new routes (scoped at `/_vercel/speed-insights/*`) after your next deployment.

### 2. Add `@vercel/speed-insights` to your project

Using the package manager of your choice, add the `@vercel/speed-insights` package to your project:

**pnpm:**
```bash
pnpm i @vercel/speed-insights
```

**yarn:**
```bash
yarn i @vercel/speed-insights
```

**npm:**
```bash
npm i @vercel/speed-insights
```

**bun:**
```bash
bun i @vercel/speed-insights
```

> **💡 Note:** When using the HTML implementation, there is no need to install the `@vercel/speed-insights` package.

## Framework-Specific Implementation

### Next.js (Pages Router)

The `SpeedInsights` component is a wrapper around the tracking script, offering more seamless integration with Next.js.

Add the following component to your main app file:

**TypeScript (pages/_app.tsx):**
```typescript
import type { AppProps } from 'next/app';
import { SpeedInsights } from '@vercel/speed-insights/next';

function MyApp({ Component, pageProps }: AppProps) {
  return (
    <>
      <Component {...pageProps} />
      <SpeedInsights />
    </>
  );
}

export default MyApp;
```

**JavaScript (pages/_app.jsx):**
```javascript
import { SpeedInsights } from "@vercel/speed-insights/next";

function MyApp({ Component, pageProps }) {
  return (
    <>
      <Component {...pageProps} />
      <SpeedInsights />
    </>
  );
}

export default MyApp;
```

**For Next.js versions older than 13.5:**

Import the `<SpeedInsights>` component from `@vercel/speed-insights/react` and pass it the pathname of the route:

```typescript
import { SpeedInsights } from "@vercel/speed-insights/react";
import { useRouter } from "next/router";

export default function Layout() {
  const router = useRouter();
  return <SpeedInsights route={router.pathname} />;
}
```

### Next.js (App Router)

Add the following component to the root layout:

**TypeScript (app/layout.tsx):**
```typescript
import { SpeedInsights } from "@vercel/speed-insights/next";

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <head>
        <title>Next.js</title>
      </head>
      <body>
        {children}
        <SpeedInsights />
      </body>
    </html>
  );
}
```

**JavaScript (app/layout.jsx):**
```javascript
import { SpeedInsights } from "@vercel/speed-insights/next";

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <head>
        <title>Next.js</title>
      </head>
      <body>
        {children}
        <SpeedInsights />
      </body>
    </html>
  );
}
```

**For Next.js versions older than 13.5:**

Create a dedicated component to avoid opting out from SSR on the layout:

**app/insights.tsx:**
```typescript
"use client";

import { SpeedInsights } from "@vercel/speed-insights/react";
import { usePathname } from "next/navigation";

export function Insights() {
  const pathname = usePathname();
  return <SpeedInsights route={pathname} />;
}
```

Then, import the `Insights` component in your layout:

```typescript
import type { ReactNode } from "react";
import { Insights } from "./insights";

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <head>
        <title>Next.js</title>
      </head>
      <body>
        {children}
        <Insights />
      </body>
    </html>
  );
}
```

### React (Create React App)

The `SpeedInsights` component is a wrapper around the tracking script, offering more seamless integration with React.

**App.tsx:**
```typescript
import { SpeedInsights } from '@vercel/speed-insights/react';

export default function App() {
  return (
    <div>
      {/* ... */}
      <SpeedInsights />
    </div>
  );
}
```

### Remix

The `SpeedInsights` component is a wrapper around the tracking script, offering a seamless integration with Remix.

**app/root.tsx:**
```typescript
import { SpeedInsights } from '@vercel/speed-insights/remix';

export default function App() {
  return (
    <html lang="en">
      <body>
        {/* ... */}
        <SpeedInsights />
      </body>
    </html>
  );
}
```

### SvelteKit

Add the following to your root file:

**src/routes/+layout.ts:**
```typescript
import { injectSpeedInsights } from "@vercel/speed-insights/sveltekit";

injectSpeedInsights();
```

### HTML

Add the following scripts before the closing tag of the `<body>`:

```html
<script>
  window.si = window.si || function () { (window.siq = window.siq || []).push(arguments); };
</script>
<script defer src="/_vercel/speed-insights/script.js"></script>
```

### Vue

The `SpeedInsights` component is a wrapper around the tracking script, offering more seamless integration with Vue.

**src/App.vue:**
```vue
<script setup lang="ts">
import { SpeedInsights } from '@vercel/speed-insights/vue';
</script>

<template>
  <SpeedInsights />
</template>
```

### Nuxt

The `SpeedInsights` component is a wrapper around the tracking script, offering more seamless integration with Nuxt.

**layouts/default.vue:**
```vue
<script setup lang="ts">
import { SpeedInsights } from '@vercel/speed-insights/vue';
</script>

<template>
  <SpeedInsights />
</template>
```

### Astro

Speed Insights is available for both static and SSR Astro apps.

To enable this feature, declare the `<SpeedInsights />` component from `@vercel/speed-insights/astro` near the bottom of one of your layout components, such as `BaseHead.astro`:

**BaseHead.astro:**
```astro
---
import SpeedInsights from '@vercel/speed-insights/astro';
const { title, description } = Astro.props;
---
<title>{title}</title>
<meta name="title" content={title} />
<meta name="description" content={description} />

<SpeedInsights />
```

**Optional: Remove sensitive information from URLs**

You can add a `speedInsightsBeforeSend` function to the global `window` object:

```astro
---
import SpeedInsights from '@vercel/speed-insights/astro';
const { title, description } = Astro.props;
---
<title>{title}</title>
<meta name="title" content={title} />
<meta name="description" content={description} />

<script is:inline>
  function speedInsightsBeforeSend(data){
    console.log("Speed Insights before send", data)
    return data;
  }
</script>
<SpeedInsights />
```

[Learn more about `beforeSend`](/docs/speed-insights/package#beforesend).

### Other Frameworks

Import the `injectSpeedInsights` function from the package, which will add the tracking script to your app. **This should only be called once in your app, and must run in the client**.

**main.ts:**
```typescript
import { injectSpeedInsights } from "@vercel/speed-insights";

injectSpeedInsights();
```

## Deployment

### Deploy your app to Vercel

You can deploy your app to Vercel's global CDN by running the following command from your terminal:

```bash
vercel deploy
```

Alternatively, you can [connect your project's git repository](/docs/git#deploying-a-git-repository), which will enable Vercel to deploy your latest pushes and merges to main.

Once your app is deployed, it's ready to begin tracking performance metrics.

> **💡 Note:** If everything is set up correctly, you should be able to find the `/_vercel/speed-insights/script.js` script inside the body tag of your page.

### View your data in the dashboard

Once your app is deployed, and users have visited your site, you can view the data in the dashboard.

To do so, go to your [dashboard](/dashboard), select your project, and click the **Speed Insights** tab.

After a few days of visitors, you'll be able to start exploring your metrics. For more information on how to use Speed Insights, see [Using Speed Insights](/docs/speed-insights/using-speed-insights).

## Privacy and Compliance

Learn more about how Vercel supports [privacy and data compliance standards](/docs/speed-insights/privacy-policy) with Vercel Speed Insights.

## Next steps

Now that you have Vercel Speed Insights set up, you can explore the following topics to learn more:

- [Learn how to use the `@vercel/speed-insights` package](/docs/speed-insights/package)
- [Learn about metrics](/docs/speed-insights/metrics)
- [Read about privacy and compliance](/docs/speed-insights/privacy-policy)
- [Explore pricing](/docs/speed-insights/limits-and-pricing)
- [Troubleshooting](/docs/speed-insights/troubleshooting)
"""
    
    content_data = {
        'category_id': str(category_id),
        'folder_id': None,
        'title': 'Getting started with Speed Insights',
        'text': content_text,
        'media_url': '',
        'media_type': 'text',
        'caption': 'A comprehensive guide to getting started with Vercel Speed Insights',
        'created_at': datetime.utcnow()
    }
    
    result = content_collection.insert_one(content_data)
    print(f"Created content 'Getting started with Speed Insights' with ID: {result.inserted_id}")
    return result.inserted_id

def main():
    """Main function to seed Speed Insights documentation"""
    print("Starting Speed Insights documentation seed...")
    
    # Create category
    category_id = create_speed_insights_category()
    
    # Create getting started guide
    content_id = create_getting_started_content(category_id)
    
    print("\nSpeed Insights documentation seeded successfully!")
    print(f"Category ID: {category_id}")
    print(f"Content ID: {content_id}")

if __name__ == "__main__":
    main()
