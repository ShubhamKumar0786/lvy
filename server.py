from flask import Flask, request, Response, send_from_directory, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import json
import time
import os
import sys
import gc

# Load environment variables from .env file
load_dotenv()

# Get the directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Add parent directory to path for imports
sys.path.insert(0, SCRIPT_DIR)

from backend import (
    SignalVinAutomation,
    parse_price,
    format_currency
)

app = Flask(__name__, static_folder=SCRIPT_DIR, static_url_path='')
CORS(app)

# Global state
automation = None
is_processing = False
should_stop = False

# ============================================
# STATIC FILE SERVING
# ============================================
@app.route('/')
def index():
    return send_from_directory(SCRIPT_DIR, 'index.html')

@app.route('/styles.css')
def serve_css():
    return send_from_directory(SCRIPT_DIR, 'styles.css', mimetype='text/css')

@app.route('/app.js')
def serve_js():
    return send_from_directory(SCRIPT_DIR, 'app.js', mimetype='application/javascript')

@app.route('/<path:filename>')
def static_files(filename):
    return send_from_directory(SCRIPT_DIR, filename)

# ============================================
# API ENDPOINTS
# ============================================
@app.route('/api/process', methods=['POST'])
def process_vins():
    global automation, is_processing, should_stop
    
    if is_processing:
        return jsonify({'error': 'Processing already in progress'}), 400
    
    config = request.json
    is_processing = True
    should_stop = False
    
    def generate():
        global automation, is_processing, should_stop
        
        try:
            valid_rows = config.get('valid_rows', [])
            total = len(valid_rows)
            
            if total == 0:
                yield f"data: {json.dumps({'type': 'error', 'message': 'No valid VINs to process'})}\n\n"
                return
            
            # Show total vehicles count
            yield f"data: {json.dumps({'type': 'log', 'message': f'📊 Total vehicles to process: {total}', 'level': 'info'})}\n\n"
            
            # Initialize automation
            yield f"data: {json.dumps({'type': 'log', 'message': '🌐 Starting browser...', 'level': 'info'})}\n\n"
            
            headless = True  # Always headless on server
            automation = SignalVinAutomation(headless=headless)
            automation.start()
            
            # Login
            yield f"data: {json.dumps({'type': 'log', 'message': '🔐 Logging in to Signal.vin...', 'level': 'info'})}\n\n"
            
            login_success = False
            attempt = 0
            
            # Get credentials from environment
            signal_email = os.getenv('SIGNAL_EMAIL')
            signal_password = os.getenv('SIGNAL_PASSWORD')
            
            # Debug: Check if credentials are loaded
            yield f"data: {json.dumps({'type': 'log', 'message': f'📧 Email loaded: {bool(signal_email)}', 'level': 'info'})}\n\n"
            yield f"data: {json.dumps({'type': 'log', 'message': f'🔑 Password loaded: {bool(signal_password)}', 'level': 'info'})}\n\n"
            
            if not signal_email or not signal_password:
                yield f"data: {json.dumps({'type': 'error', 'message': '❌ SIGNAL_EMAIL or SIGNAL_PASSWORD not set in environment!'})}\n\n"
                return
            
            # Create a simple callback for logging
            def login_callback(msg_type, msg):
                pass  # Silent callback for server mode
            
            while not login_success and not should_stop and attempt < 10:
                attempt += 1
                yield f"data: {json.dumps({'type': 'log', 'message': f'🔐 Login attempt {attempt}...', 'level': 'info'})}\n\n"
                
                try:
                    if automation.auto_login(signal_email, signal_password, login_callback):
                        login_success = True
                        yield f"data: {json.dumps({'type': 'log', 'message': '✅ Login successful!', 'level': 'success'})}\n\n"
                except Exception as e:
                    yield f"data: {json.dumps({'type': 'log', 'message': f'⚠️ Login error: {str(e)}', 'level': 'warning'})}\n\n"
                    time.sleep(2)
            
            if should_stop:
                yield f"data: {json.dumps({'type': 'log', 'message': '⏹️ Processing stopped', 'level': 'warning'})}\n\n"
                return
            
            # Process each VIN
            success_count = 0
            error_count = 0
            all_results = []
            
            for i, item in enumerate(valid_rows):
                if should_stop:
                    break
                
                # Restart browser every 3 VINs to free memory
                if i > 0 and i % 3 == 0:
                    yield f"data: {json.dumps({'type': 'log', 'message': '🔄 Restarting browser to free memory...', 'level': 'info'})}\n\n"
                    try:
                        automation.stop()
                        gc.collect()  # Force garbage collection
                        time.sleep(1)
                        automation.start()
                        # Re-login after restart
                        automation.auto_login(signal_email, signal_password, login_callback)
                        yield f"data: {json.dumps({'type': 'log', 'message': '✅ Browser restarted!', 'level': 'success'})}\n\n"
                    except Exception as e:
                        yield f"data: {json.dumps({'type': 'log', 'message': f'⚠️ Restart error: {str(e)}', 'level': 'warning'})}\n\n"
                
                vin = item['vin']
                progress = (i + 1) / total
                
                yield f"data: {json.dumps({'type': 'progress', 'progress': progress, 'message': f'Processing VIN {i+1} of {total} | ✅ Success: {success_count} | ❌ Errors: {error_count}'})}\n\n"
                yield f"data: {json.dumps({'type': 'log', 'message': f'🔍 [{i+1}/{total}] Processing VIN: {vin}', 'level': 'info'})}\n\n"
                
                try:
                    # Create log function for this VIN
                    def log_func(msg):
                        pass  # Suppress detailed logs for performance
                    
                    result = automation.appraise_vehicle(
                        vin=item['vin'],
                        odometer=item['odometer'],
                        trim=item.get('trim', ''),
                        list_price=item.get('list_price', 0),
                        listing_url=item.get('listing_url', ''),
                        carfax_link=item.get('carfax_link', ''),
                        make=item.get('make', ''),
                        model=item.get('model', ''),
                        year=item.get('year', ''),
                        log_func=log_func
                    )
                    
                    # Send result
                    yield f"data: {json.dumps({'type': 'result', 'result': result})}\n\n"
                    all_results.append(result)
                    
                    if result.get('export_value_cad'):
                        success_count += 1
                        export_val = result['export_value_cad']
                        yield f"data: {json.dumps({'type': 'log', 'message': f'✅ Export Value: ${export_val} CAD', 'level': 'success'})}\n\n"
                        
                        if result.get('profit') and result['profit'] > 0:
                            profit_msg = format_currency(result['profit'])
                            yield f"data: {json.dumps({'type': 'log', 'message': f'💰 PROFIT: {profit_msg}', 'level': 'success'})}\n\n"
                        elif result.get('profit'):
                            loss_msg = format_currency(result['profit'])
                            yield f"data: {json.dumps({'type': 'log', 'message': f'📉 LOSS: {loss_msg}', 'level': 'warning'})}\n\n"
                    else:
                        yield f"data: {json.dumps({'type': 'log', 'message': f'⚠️ No export value found for {vin}', 'level': 'warning'})}\n\n"
                    
                except Exception as e:
                    error_count += 1
                    yield f"data: {json.dumps({'type': 'log', 'message': f'❌ Error processing {vin}: {str(e)}', 'level': 'error'})}\n\n"
            
            # Complete
            yield f"data: {json.dumps({'type': 'complete', 'message': f'Completed! ✅ Success: {success_count} | ❌ Errors: {error_count}'})}\n\n"
            
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        
        finally:
            is_processing = False
            if automation:
                try:
                    automation.stop()
                except:
                    pass
                automation = None
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/api/stop', methods=['POST'])
def stop_processing():
    global should_stop
    should_stop = True
    return jsonify({'status': 'stopping'})

@app.route('/api/status', methods=['GET'])
def get_status():
    return jsonify({
        'is_processing': is_processing
    })

# ============================================
# MAIN
# ============================================
if __name__ == '__main__':
    print("🚀 Starting Signal.vin Bulk Appraisal Server...")
    print("📍 Open http://localhost:8000 in your browser")
    app.run(host='0.0.0.0', port=8000, debug=False, threaded=True)
