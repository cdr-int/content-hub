# Content Hub - Flask Templates

This package contains all the HTML templates needed to work with your Flask backend.

## 🆕 Recent Updates

### Sidebar Navigation
The sidebar now displays:
- **Search bar** at the top for filtering categories
- **Categories** sorted with **Premium first** (alphabetically), then **Free** (alphabetically)
- Categories only show if the user has permission to view them
- Clean, modern design matching your reference image

## Files Included

- **base.html** - Base template with navigation, header, and sidebar with categories
- **index.html** - Home page (landing page for non-logged users, welcome page for logged users)
- **login.html** - Login page
- **register.html** - Registration page
- **categories.html** - Categories listing page
- **category_detail.html** - Individual category page with content items
- **admin.html** - Admin panel for user and category management
- **app.py** - Updated Flask backend with sidebar category support

## Installation

1. Place all these HTML files in your Flask project's `templates/` directory:
   ```
   your-project/
   ├── app.py (your Flask backend)
   ├── templates/
   │   ├── base.html
   │   ├── index.html
   │   ├── login.html
   │   ├── register.html
   │   ├── categories.html
   │   ├── category_detail.html
   │   └── admin.html
   └── requirements.txt
   ```

2. Make sure your Flask backend has the correct imports and configuration:
   ```python
   from flask import Flask, render_template, request, jsonify, session, redirect, url_for
   
   app = Flask(__name__)
   app.secret_key = 'your-secret-key'
   ```

## Features

### User Features
- User registration and login
- **Smart sidebar navigation** with search
- **Automatic category sorting** (Premium first, then Free, all alphabetically)
- View free and premium categories (based on subscription)
- Browse content items (images, videos, text)
- Searchable category list in sidebar and main page

### Admin Features
- User management (toggle subscriptions, delete users)
- Category management (create, edit, delete categories with custom colors)
- Content management (add, edit, delete content items)
- Home page customization
- Real-time search for users and categories

## Technology Stack

- **Frontend**: HTML, Tailwind CSS (via CDN), Vanilla JavaScript
- **Backend**: Flask, PyMongo
- **Database**: MongoDB
- **Icons**: Lucide Icons (SVG)

## Key Implementation Details

### Sidebar Category Sorting
The sidebar automatically sorts categories using a Flask context processor:
1. **Premium categories** first (alphabetically)
2. **Free categories** second (alphabetically)
3. Only shows categories the user has permission to view
4. Updates automatically when categories are added/removed

The sorting is handled in `app.py` with the `get_accessible_categories()` function and `@app.context_processor` decorator, making categories available to all templates as `sidebar_categories`.

## Key Features

### Responsive Design
All templates are fully responsive and work on mobile, tablet, and desktop devices.

### Color Customization
Categories support custom accent colors using hex codes (e.g., #4F46E5).

### Content Types
Support for three content types:
- **Text**: Text-only content
- **Image**: Image with optional caption
- **Video**: Video with optional caption

### Security
- Password hashing with Werkzeug
- Session-based authentication
- Admin-only routes protected with decorators

## Database Structure

### Users Collection
```json
{
  "_id": ObjectId,
  "username": "string",
  "password": "hashed_password",
  "is_admin": boolean,
  "is_subscribed": boolean,
  "created_at": datetime
}
```

### Categories Collection
```json
{
  "_id": ObjectId,
  "name": "string",
  "description": "string",
  "is_free": boolean,
  "accent_color": "#RRGGBB",
  "created_at": datetime
}
```

### Content Collection
```json
{
  "_id": ObjectId,
  "category_id": "string",
  "title": "string",
  "text": "string",
  "media_url": "string",
  "media_type": "text|image|video",
  "caption": "string",
  "created_at": datetime
}
```

### Pages Collection
```json
{
  "_id": ObjectId,
  "page_name": "home",
  "accent_color": "#RRGGBB",
  "title": "string",
  "description": "string",
  "preview_image": "string"
}
```

## API Endpoints

All API endpoints are already implemented in your Flask backend:

- **GET** `/api/users` - Get all users (admin only)
- **PUT** `/api/users/<user_id>` - Update user (admin only)
- **DELETE** `/api/users/<user_id>` - Delete user (admin only)
- **GET** `/api/categories` - Get all categories
- **POST** `/api/categories` - Create category (admin only)
- **PUT** `/api/categories/<category_id>` - Update category (admin only)
- **DELETE** `/api/categories/<category_id>` - Delete category (admin only)
- **POST** `/api/content` - Create content (admin only)
- **PUT** `/api/content/<content_id>` - Update content (admin only)
- **DELETE** `/api/content/<content_id>` - Delete content (admin only)
- **GET** `/api/pages/<page_name>` - Get page settings
- **PUT** `/api/pages/<page_name>` - Update page settings (admin only)

## Running the Application

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set environment variables:
   ```bash
   export SECRET_KEY='your-secret-key-here'
   ```

3. Run the Flask app:
   ```bash
   python app.py
   ```

4. Access the application:
   - Open browser to `http://localhost:5000`
   - Use demo credentials or register a new account

## Demo Credentials

If you want to create demo users in MongoDB:

**Admin Account:**
- Username: `admin`
- Password: `admin123` (hashed)

**Regular User:**
- Username: `john_doe`
- Password: `pass123` (hashed)

## Customization

### Changing Colors
Edit the hex color codes in:
- Category accent colors (admin panel)
- Home page accent color (admin panel)
- Tailwind color classes in templates

### Adding New Pages
1. Create a new template extending `base.html`
2. Add route in Flask backend
3. Add navigation link in `base.html` menu

### Styling
All styling uses Tailwind CSS. You can:
- Modify classes directly in templates
- Add custom CSS in `<style>` blocks
- Use Tailwind configuration for custom themes

## Troubleshooting

### Templates not found
- Ensure templates are in the `templates/` directory
- Check Flask is looking in the right directory

### MongoDB connection errors
- Verify MongoDB connection string
- Check network access and credentials

### Static files not loading
- Tailwind CSS loads from CDN (requires internet)
- Check browser console for errors

## License

This project is provided as-is for your use.

## Documentation

For additional guides and documentation, check out the [docs/](docs/) directory:

- [Getting Started with Vercel Web Analytics](docs/analytics/getting-started-with-web-analytics.md) - Comprehensive guide for setting up Vercel Web Analytics across multiple frameworks

## Support

For issues or questions:
1. Check MongoDB Atlas connection
2. Verify all dependencies are installed
3. Check browser console for JavaScript errors
4. Review Flask logs for backend errors
