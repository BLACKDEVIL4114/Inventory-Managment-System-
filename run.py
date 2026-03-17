#!/usr/bin/env python3
"""
Startup script for the Inventory Management System
"""

import sys
import os

def check_dependencies():
    """Check if required dependencies are installed"""
    required_packages = ['flask', 'pandas', 'numpy', 'matplotlib', 'sklearn']
    missing_packages = []
    
    for package in required_packages:
        try:
            __import__(package)
        except ImportError:
            missing_packages.append(package)
    
    if missing_packages:
        print("Missing required packages:", missing_packages)
        print("Please install them using: pip install " + " ".join(missing_packages))
        return False
    return True

def main():
    """Main function to start the application"""
    print("Inventory Management System")
    print("=" * 40)
    
    # Check dependencies
    if not check_dependencies():
        sys.exit(1)
    
    # Check if data file exists
    if not os.path.exists('data_set/data.csv'):
        print("Warning: data_set/data.csv not found.")
        print("Please upload a CSV file through the web interface.")
    
    # Check if model exists
    if not os.path.exists('trained_model.pkl'):
        print("Info: No trained model found.")
        print("You can train a model through the web interface.")
    
    print("\nStarting Flask application...")
    print("Access the application at: http://localhost:5000")
    print("Press Ctrl+C to stop the server")
    print("-" * 40)
    
    try:
        from app import app
        app.run(debug=True, host='0.0.0.0', port=5000,
                use_reloader=True, reloader_type='stat')
    except KeyboardInterrupt:
        print("\nServer stopped by user.")
    except Exception as e:
        print(f"Error starting application: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main() 