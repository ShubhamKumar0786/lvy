import requests
import json
import time
import re
import os
from playwright.sync_api import sync_playwright

# ============================================
# HELPER FUNCTIONS
# ============================================

def is_valid_vin_for_export(vin: str, valid_prefixes: tuple) -> bool:
    if not vin or len(vin) < 17:
        return False
    return vin.strip().upper().startswith(valid_prefixes)

def parse_price(price_str: str) -> float:
    if not price_str:
        return 0.0
    try:
        cleaned = re.sub(r'[^\d.-]', '', str(price_str))
        return float(cleaned) if cleaned else 0.0
    except:
        return 0.0

def format_currency(amount: float) -> str:
    if amount >= 0:
        return f"${amount:,.0f}"
    else:
        return f"-${abs(amount):,.0f}"

def get_supabase_headers(api_key: str):
    return {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

# ============================================
# SUPABASE OPERATIONS
# ============================================

def fetch_sheet_data(supabase_url: str, api_key: str, table_name: str) -> list:
    all_data = []
    batch_size = 1000
    offset = 0

    try:
        while True:
            url = f"{supabase_url}/rest/v1/{table_name}?select=*&limit={batch_size}&offset={offset}"
            response = requests.get(url, headers=get_supabase_headers(api_key), timeout=60)
            response.raise_for_status()
            batch = response.json()

            if not batch:
                break

            all_data.extend(batch)

            if len(batch) < batch_size:
                break

            offset += batch_size

        return all_data
    except Exception as e:
        raise Exception(f"Error fetching data from Supabase: {e}")

def update_sheet_row(supabase_url: str, api_key: str, table_name: str, vin_column: str, export_column: str, vin: str, export_value: str) -> bool:
    try:
        url = f"{supabase_url}/rest/v1/{table_name}?{vin_column}=eq.{vin}"
        payload = {export_column: export_value}
        response = requests.patch(url, json=payload, headers=get_supabase_headers(api_key), timeout=30)
        return response.status_code in [200, 204]
    except:
        return False

def save_to_appraisal_results(supabase_url: str, api_key: str, result: dict, log_func=None) -> bool:
    """Save appraisal result to appraisal_results table"""
    try:
        url = f"{supabase_url}/rest/v1/appraisal_results"

        # Handle export_value - convert string to float properly
        export_val = None
        if result.get('export_value_cad'):
            try:
                clean_val = str(result['export_value_cad']).replace(',', '').replace('$', '').strip()
                export_val = float(clean_val)
            except:
                export_val = None

        # Get listing_url value - DIRECTLY from result
        listing_url_val = result.get('listing_url') or ''

        # Handle price
        price_val = result.get('list_price', 0)
        if isinstance(price_val, str):
            price_val = parse_price(price_val)

        # Handle profit
        profit_val = result.get('profit')
        if profit_val is not None:
            try:
                profit_val = float(profit_val)
            except:
                profit_val = None

        # Get carfax_link value
        carfax_link_val = result.get('carfax_link') or ''
        
        # Get make, model from inventory, trim from Signal.vin
        make_val = result.get('make') or ''      # from inventory
        model_val = result.get('model') or ''    # from inventory
        trim_val = result.get('signal_trim') or ''  # from Signal.vin

        payload = {
            "vin": result.get('vin', ''),
            "kilometers": str(result.get('odometer', '')),
            "listing_link": listing_url_val,   # comes from inventory
            "carfax_link": carfax_link_val,    # comes from inventory
            "make": make_val,                  # comes from inventory
            "model": model_val,                # comes from inventory
            "trim": trim_val,                  # comes from Signal.vin
            "price": price_val,
            "export_value": export_val,        # comes from Signal.vin (Export value)
            "profit": profit_val,
            "status": result.get('status', '')
        }

        # Debug output
        if log_func:
            log_func(f"💾 **Saving to DB:** VIN={payload['vin']}")
            log_func(f"   - make: `{make_val}` (from inventory)")
            log_func(f"   - model: `{model_val}` (from inventory)")
            log_func(f"   - trim: `{trim_val}` (from Signal.vin)")
            log_func(f"   - listing_link: `{listing_url_val}`")
            log_func(f"   - carfax_link: `{carfax_link_val}`")
            log_func(f"   - export_value: `{export_val}`")
            log_func(f"   - price: `{price_val}`")
            log_func(f"   - profit: `{profit_val}`")

        response = requests.post(url, json=payload, headers=get_supabase_headers(api_key), timeout=30)

        if response.status_code not in [200, 201]:
            error_msg = f"Save failed for {result.get('vin')}: {response.status_code} - {response.text}"
            if log_func:
                log_func(f"⚠️ {error_msg}")
            return False

        if log_func:
            log_func(f"✅ Saved to appraisal_results!")
        return True

    except Exception as e:
        if log_func:
            log_func(f"⚠️ Save error: {e}")
        return False

# ============================================
# BROWSER AUTOMATION CLASS
# ============================================

class SignalVinAutomation:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.browser = None
        self.page = None
        self.context = None
        self.playwright = None
        self.logged_in = False
        self.signal_url = "https://app.signal.vin"
        self.captured_responses = []  # Store API responses
        self.vehicle_make = ''
        self.vehicle_model = ''
        self.vehicle_trim = ''

    def start(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=self.headless, slow_mo=0)
        self.context = self.browser.new_context(
            viewport={'width': 1520, 'height': 960},
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            has_touch=True  # Enable touch for checkbox clicking
        )
        self.page = self.context.new_page()
        
        # Set up network response listener
        self.page.on("response", self._capture_response)
        
        return True

    def _capture_response(self, response):
        """Capture API responses that might contain export value"""
        try:
            url = response.url
            # Capture all signal.vin API responses
            if 'signal.vin' in url or 'export' in url.lower():
                try:
                    body = response.text()
                    self.captured_responses.append({
                        'url': url,
                        'status': response.status,
                        'body': body if body else ''  # Full body
                    })
                except:
                    pass
        except:
            pass

    def stop(self):
        try:
            if self.browser:
                self.browser.close()
        except:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except:
            pass

    def click_checkbox(self, status_callback=None) -> bool:
        """
        Click the 'I agree' checkbox on Signal.vin login page using multiple methods
        Returns True if checkbox was successfully checked
        """
        def log(msg):
            if status_callback:
                status_callback("info", msg)
        
        log("🔲 Attempting to click checkbox...")
        
        # Get page dimensions
        try:
            box = self.page.locator('flt-glass-pane').first.bounding_box()
            log(f"   Glass pane: {box['width']:.0f} x {box['height']:.0f}")
        except:
            box = {'x': 0, 'y': 0, 'width': 1280, 'height': 800}
        
        # Find checkbox element in semantics
        checkbox_elem = None
        checkbox_box = None
        
        try:
            semantics = self.page.locator('flt-semantics').all()
            log(f"   Found {len(semantics)} semantic elements")
            
            for i, elem in enumerate(semantics):
                try:
                    role = elem.get_attribute('role') or ''
                    label = elem.get_attribute('aria-label') or ''
                    checked = elem.get_attribute('aria-checked') or ''
                    
                    if role == 'checkbox':
                        checkbox_elem = elem
                        checkbox_box = elem.bounding_box()
                        log(f"   🎯 Found checkbox element! checked='{checked}'")
                        break
                except:
                    continue
        except Exception as e:
            log(f"   ⚠️ Error finding semantics: {e}")
        
        # Calculate click position
        if checkbox_box:
            cx = checkbox_box['x'] + checkbox_box['width'] / 2
            cy = checkbox_box['y'] + checkbox_box['height'] / 2
            log(f"   Using detected checkbox position: ({cx:.0f}, {cy:.0f})")
        else:
            # Estimate based on typical Flutter layout
            cx = box['x'] + box['width'] * 0.41  # Slightly left of center
            cy = box['y'] + box['height'] * 0.58  # Below password field
            log(f"   Using estimated position: ({cx:.0f}, {cy:.0f})")
        
        methods_tried = []
        
        # METHOD 1: Direct element click (if found)
        if checkbox_elem:
            log("   → Method 1: Direct element click...")
            try:
                checkbox_elem.click(force=True)
                time.sleep(0.5)
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('Direct element', checked == 'true'))
                if checked == 'true':
                    log("   ✅ Direct click worked!")
                    return True
            except Exception as e:
                log(f"   Failed: {e}")
                methods_tried.append(('Direct element', False))
        
        # METHOD 2: CDP click
        log("   → Method 2: CDP click...")
        try:
            cdp = self.context.new_cdp_session(self.page)
            cdp.send('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': int(cx), 'y': int(cy), 'button': 'left', 'clickCount': 1})
            time.sleep(0.05)
            cdp.send('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': int(cx), 'y': int(cy), 'button': 'left', 'clickCount': 1})
            time.sleep(0.5)
            if checkbox_elem:
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('CDP', checked == 'true'))
                if checked == 'true':
                    log("   ✅ CDP click worked!")
                    return True
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('CDP', False))
        
        # METHOD 3: JavaScript pointer events
        log("   → Method 3: JavaScript pointer events...")
        try:
            self.page.evaluate(f'''() => {{
                const target = document.querySelector('flt-glass-pane');
                const rect = target.getBoundingClientRect();
                
                const evt = new PointerEvent('pointerdown', {{
                    bubbles: true,
                    cancelable: true,
                    view: window,
                    clientX: {cx},
                    clientY: {cy},
                    pointerId: 1,
                    pointerType: 'mouse',
                    isPrimary: true,
                    button: 0,
                    buttons: 1
                }});
                target.dispatchEvent(evt);
                
                setTimeout(() => {{
                    const upEvt = new PointerEvent('pointerup', {{
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        clientX: {cx},
                        clientY: {cy},
                        pointerId: 1,
                        pointerType: 'mouse',
                        isPrimary: true,
                        button: 0,
                        buttons: 0
                    }});
                    target.dispatchEvent(upEvt);
                }}, 30);
            }}''')
            time.sleep(0.3)
            if checkbox_elem:
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('JS Events', checked == 'true'))
                if checked == 'true':
                    log("   ✅ JS pointer events worked!")
                    return True
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('JS Events', False))
        
        # METHOD 4: Playwright mouse click
        log("   → Method 4: Playwright mouse click...")
        try:
            self.page.mouse.click(cx, cy)
            time.sleep(0.3)
            if checkbox_elem:
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('Mouse', checked == 'true'))
                if checked == 'true':
                    log("   ✅ Mouse click worked!")
                    return True
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('Mouse', False))
        
        # METHOD 5: Touch tap
        log("   → Method 5: Touch tap...")
        try:
            self.page.touchscreen.tap(cx, cy)
            time.sleep(0.3)
            if checkbox_elem:
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('Touch', checked == 'true'))
                if checked == 'true':
                    log("   ✅ Touch tap worked!")
                    return True
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('Touch', False))
        
        # METHOD 6: Keyboard Tab + Space
        log("   → Method 6: Keyboard Tab + Space...")
        try:
            self.page.keyboard.press('Tab')
            time.sleep(0.1)
            self.page.keyboard.press('Space')
            time.sleep(0.3)
            if checkbox_elem:
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('Tab+Space', checked == 'true'))
                if checked == 'true':
                    log("   ✅ Tab+Space worked!")
                    return True
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('Tab+Space', False))
        
        # METHOD 7: Click on "I agree" text area
        log("   → Method 7: Click on 'I agree' text area...")
        try:
            text_x = cx + 100
            text_y = cy
            self.page.mouse.click(text_x, text_y)
            time.sleep(0.3)
            if checkbox_elem:
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('Text click', checked == 'true'))
                if checked == 'true':
                    log("   ✅ Text click worked!")
                    return True
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('Text click', False))
        
        # METHOD 8: get_by_role checkbox
        log("   → Method 8: get_by_role('checkbox')...")
        try:
            cb = self.page.get_by_role('checkbox')
            if cb.count() > 0:
                cb.first.check(force=True)
                time.sleep(0.3)
                checked = cb.first.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('get_by_role', checked == 'true'))
                if checked == 'true':
                    log("   ✅ get_by_role worked!")
                    return True
            else:
                log("   No checkbox found via get_by_role")
                methods_tried.append(('get_by_role', False))
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('get_by_role', False))
        
        # Final verification
        log("🔍 Final checkbox state check...")
        if checkbox_elem:
            final_checked = checkbox_elem.get_attribute('aria-checked')
            log(f"   aria-checked = '{final_checked}'")
            if final_checked == 'true':
                log("✅ CHECKBOX IS CHECKED!")
                return True
            else:
                log("❌ Checkbox still unchecked after all methods")
        
        log("⚠️ All checkbox click methods attempted")
        return False

    def click_login_button(self, status_callback=None) -> bool:
        """
        Click the Login button on Signal.vin login page
        Returns True if login button was clicked
        """
        def log(msg):
            if status_callback:
                status_callback("info", msg)
        
        log("🔘 Attempting to click Login button...")
        
        # Find Login button in semantics
        login_elem = None
        login_box = None
        
        try:
            semantics = self.page.locator('flt-semantics').all()
            
            for elem in semantics:
                try:
                    role = elem.get_attribute('role') or ''
                    label = elem.get_attribute('aria-label') or ''
                    
                    if role == 'button' and 'login' in label.lower():
                        login_elem = elem
                        login_box = elem.bounding_box()
                        log(f"   🎯 Found Login button!")
                        break
                except:
                    continue
        except Exception as e:
            log(f"   ⚠️ Error finding Login button: {e}")
        
        # METHOD 1: Direct element click (if found)
        if login_elem:
            try:
                login_elem.click(force=True)
                time.sleep(0.5)
                log("   ✅ Login button clicked (direct)!")
                return True
            except Exception as e:
                log(f"   ⚠️ Direct click failed: {e}")
        
        # METHOD 2: Click by position
        if login_box:
            try:
                cx = login_box['x'] + login_box['width'] / 2
                cy = login_box['y'] + login_box['height'] / 2
                self.page.mouse.click(cx, cy)
                time.sleep(0.5)
                log("   ✅ Login button clicked (by position)!")
                return True
            except Exception as e:
                log(f"   ⚠️ Position click failed: {e}")
        
        # METHOD 3: Try get_by_role
        try:
            btn = self.page.get_by_role('button', name=re.compile(r'login', re.I))
            if btn.count() > 0:
                btn.first.click(force=True)
                time.sleep(0.5)
                log("   ✅ Login button clicked (get_by_role)!")
                return True
        except Exception as e:
            log(f"   ⚠️ get_by_role failed: {e}")
        
        # METHOD 4: Keyboard - Tab to Login button and press Enter
        log("   → Method 4: Keyboard Tab + Enter...")
        try:
            self.page.keyboard.press('Tab')
            time.sleep(0.2)
            self.page.keyboard.press('Enter')
            time.sleep(0.5)
            log("   ✅ Login button clicked (Tab + Enter)!")
            return True
        except Exception as e:
            log(f"   ⚠️ Keyboard failed: {e}")
        
        # METHOD 5: Click at estimated position (below checkbox)
        log("   → Method 5: Estimated position click...")
        try:
            glass_pane = self.page.locator('flt-glass-pane').first
            box = glass_pane.bounding_box()
            if box:
                # Login button is usually at center-bottom area
                btn_x = box['x'] + box['width'] * 0.5
                btn_y = box['y'] + box['height'] * 0.72
                self.page.mouse.click(btn_x, btn_y)
                time.sleep(0.5)
                log(f"   ✅ Login clicked at estimated position ({btn_x:.0f}, {btn_y:.0f})!")
                return True
        except Exception as e:
            log(f"   ⚠️ Estimated click failed: {e}")
        
        # METHOD 6: CDP click at login button area
        log("   → Method 6: CDP click...")
        try:
            if login_box:
                cx = login_box['x'] + login_box['width'] / 2
                cy = login_box['y'] + login_box['height'] / 2
            else:
                # Estimate position
                glass_pane = self.page.locator('flt-glass-pane').first
                box = glass_pane.bounding_box()
                cx = box['x'] + box['width'] * 0.5
                cy = box['y'] + box['height'] * 0.72
            
            cdp = self.context.new_cdp_session(self.page)
            cdp.send('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': int(cx), 'y': int(cy), 'button': 'left', 'clickCount': 1})
            time.sleep(0.05)
            cdp.send('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': int(cx), 'y': int(cy), 'button': 'left', 'clickCount': 1})
            time.sleep(0.5)
            log("   ✅ Login clicked via CDP!")
            return True
        except Exception as e:
            log(f"   ⚠️ CDP click failed: {e}")
        
        log("⚠️ Could not click Login button - all methods tried")
        return False

    def select_export_product(self, status_callback=None) -> bool:
        """
        Select 'Export' product from the product selection dialog after login
        Step 1: Click Export radio
        Step 2: Click Continue button
        Uses KEYBOARD navigation
        """
        def log(msg):
            if status_callback:
                status_callback("info", msg)
        
        log("📦 Checking for product selection dialog...")
        time.sleep(3)  # Wait for dialog to appear
        
        # Check if dialog is visible
        dialog_found = False
        try:
            semantics = self.page.locator('flt-semantics').all()
            for elem in semantics:
                try:
                    label = elem.get_attribute('aria-label') or ''
                    if 'select product' in label.lower() or 'marketplace' in label.lower():
                        dialog_found = True
                        break
                except:
                    continue
        except:
            pass
        
        if not dialog_found:
            log("   No product selection dialog found - continuing...")
            return True
        
        log("   🎯 Product selection dialog detected!")
        
        # ===== STEP 1: SELECT EXPORT RADIO =====
        log("   📻 Step 1: Clicking on Export...")
        
        # Tab Tab to go directly to Export (skip Marketplace), then Space to click
        try:
            self.page.keyboard.press('Tab')  # Focus on Marketplace
            time.sleep(0.15)
            self.page.keyboard.press('Tab')  # Move to Export
            time.sleep(0.15)
            self.page.keyboard.press('Space')  # Click Export
            time.sleep(0.3)
            log("   ✅ Export clicked! (Tab + Tab + Space)")
        except Exception as e:
            log(f"   ⚠️ Keyboard select failed: {e}")
        
        time.sleep(0.3)
        
        # ===== STEP 2: CLICK CONTINUE BUTTON =====
        log("   🔘 Step 2: Clicking on Continue...")
        
        # Tab to Continue button and press Enter to click it
        try:
            self.page.keyboard.press('Tab')  # Move to Cancel button
            time.sleep(0.1)
            self.page.keyboard.press('Tab')  # Move to Continue button
            time.sleep(0.1)
            self.page.keyboard.press('Enter')  # Click Continue
            time.sleep(0.5)
            log("   ✅ Continue clicked! (Tab + Tab + Enter)")
        except Exception as e:
            log(f"   ⚠️ Method 1 failed: {e}")
        
        # Check if dialog closed
        time.sleep(0.5)
        dialog_still_open = False
        try:
            semantics = self.page.locator('flt-semantics').all()
            for elem in semantics:
                label = elem.get_attribute('aria-label') or ''
                if 'select product' in label.lower() or 'marketplace' in label.lower():
                    dialog_still_open = True
                    break
        except:
            pass
        
        if not dialog_still_open:
            log("✅ Export product selected and Continue clicked!")
            return True
        
        # If dialog still open, try again with different approach
        log("   ⚠️ Dialog still open, trying alternative...")
        
        # Alternative: Tab Tab Space for Export, then Tab Tab Enter for Continue
        try:
            self.page.keyboard.press('Tab')
            time.sleep(0.1)
            self.page.keyboard.press('Tab')  # To Export
            time.sleep(0.1)
            self.page.keyboard.press('Space')  # Select Export
            time.sleep(0.2)
            self.page.keyboard.press('Tab')
            time.sleep(0.1)
            self.page.keyboard.press('Tab')  # To Continue
            time.sleep(0.1)
            self.page.keyboard.press('Enter')  # Click Continue
            time.sleep(0.5)
            log("   ✅ Alternative keyboard done!")
        except Exception as e:
            log(f"   ⚠️ Alternative failed: {e}")
        
        # Final attempt: Direct Tab sequence to Export then Continue
        try:
            self.page.keyboard.press('Tab')
            time.sleep(0.1)
            self.page.keyboard.press('Tab')  # Export
            time.sleep(0.1)
            self.page.keyboard.press('Enter')  # Select Export
            time.sleep(0.2)
            self.page.keyboard.press('Tab')
            time.sleep(0.1)
            self.page.keyboard.press('Tab')
            time.sleep(0.1)
            self.page.keyboard.press('Enter')  # Continue
            time.sleep(0.5)
            log("   ✅ Final keyboard attempt done!")
        except Exception as e:
            log(f"   ⚠️ Final attempt failed: {e}")
        
        log("✅ Product selection completed!")
        return True

    def auto_login(self, email: str, password: str, status_callback=None, max_retries: int = 999999) -> bool:
        """Login with INFINITE retry logic - NEVER STOPS"""
        self.login_email = email
        self.login_password = password
        self.login_callback = status_callback
        
        attempt = 0
        while True:  # INFINITE LOOP - never stop trying
            attempt += 1
            try:
                if status_callback:
                    status_callback("info", f"🔐 Logging in to Signal.vin... (Attempt {attempt})")
                self.page.goto(self.signal_url, wait_until='domcontentloaded')
                time.sleep(0.3)

                if "dashboard" in self.page.url or "appraisal" in self.page.url:
                    if status_callback:
                        status_callback("success", "✅ Already logged in!")
                    self.logged_in = True
                    return True

                try:
                    login_btn = self.page.locator('a:has-text("Login"), button:has-text("Login")').first
                    if login_btn.is_visible():
                        login_btn.click()
                        time.sleep(0.3)
                except:
                    pass

                # Navigate to login page if not there
                if 'login' not in self.page.url.lower():
                    self.page.goto(f"{self.signal_url}/login", wait_until='networkidle')
                    time.sleep(0.5)
                
                # Wait for Flutter to load
                if status_callback:
                    status_callback("info", "⏳ Waiting for Flutter to load...")
                time.sleep(4)

                # ===== CLICK ON EMAIL FIELD FIRST =====
                if status_callback:
                    status_callback("info", "📧 Clicking on email field...")
                
                email_clicked = False
                
                # Method 1: Find textbox in flt-semantics
                try:
                    semantics = self.page.locator('flt-semantics').all()
                    for elem in semantics:
                        role = elem.get_attribute('role') or ''
                        if role == 'textbox':
                            elem.click(force=True)
                            email_clicked = True
                            if status_callback:
                                status_callback("info", "   ✅ Email field clicked (flt-semantics)")
                            break
                except:
                    pass
                
                # Method 2: Find by role textbox
                if not email_clicked:
                    try:
                        textboxes = self.page.get_by_role('textbox').all()
                        if len(textboxes) > 0:
                            textboxes[0].click(force=True)
                            email_clicked = True
                            if status_callback:
                                status_callback("info", "   ✅ Email field clicked (get_by_role)")
                    except:
                        pass
                
                # Method 3: Click on estimated position for email field
                if not email_clicked:
                    try:
                        glass_pane = self.page.locator('flt-glass-pane').first
                        box = glass_pane.bounding_box()
                        if box:
                            email_x = box['x'] + box['width'] * 0.5
                            email_y = box['y'] + box['height'] * 0.35
                            self.page.mouse.click(email_x, email_y)
                            email_clicked = True
                            if status_callback:
                                status_callback("info", f"   ✅ Email field clicked (position: {email_x:.0f}, {email_y:.0f})")
                    except:
                        pass
                
                time.sleep(0.2)

                # ===== TYPE EMAIL =====
                if status_callback:
                    status_callback("info", f"📧 Typing email: {email}")
                self.page.keyboard.type(email, delay=15)
                time.sleep(0.2)

                # ===== TYPE PASSWORD =====
                if status_callback:
                    status_callback("info", "🔑 Filling password...")
                self.page.keyboard.press('Tab')
                time.sleep(0.1)
                self.page.keyboard.type(password, delay=15)
                time.sleep(0.3)

                # ===== CLICK CHECKBOX =====
                if status_callback:
                    status_callback("info", "🔲 Clicking 'I agree' checkbox...")
                checkbox_clicked = self.click_checkbox(status_callback)
                
                if checkbox_clicked:
                    if status_callback:
                        status_callback("success", "✅ Checkbox clicked!")
                    time.sleep(0.5)
                    
                    # ===== CLICK LOGIN BUTTON =====
                    if status_callback:
                        status_callback("info", "🔘 Clicking Login button...")
                    self.click_login_button(status_callback)
                    
                    # ===== SELECT EXPORT PRODUCT =====
                    time.sleep(3)  # Wait for product dialog to appear
                    if status_callback:
                        status_callback("info", "📦 Selecting Export product...")
                    self.select_export_product(status_callback)
                else:
                    if status_callback:
                        status_callback("warning", "👉 Please complete login manually in the browser window (check 'I agree' and click Login)")

                try:
                    self.page.wait_for_url('**/dashboard**', timeout=600000)  # 10 minutes wait
                except:
                    try:
                        self.page.wait_for_url('**/appraisal**', timeout=60000)  # 1 minute wait
                    except:
                        pass

                if "dashboard" in self.page.url or "appraisal" in self.page.url:
                    if status_callback:
                        status_callback("success", "✅ Login successful!")
                    self.logged_in = True
                    return True

            except Exception as e:
                if status_callback:
                    status_callback("warning", f"⚠️ Login attempt {attempt} failed: {e}. Retrying...")
                time.sleep(0.5)
                continue  # NEVER STOP - keep trying
        
        # This should never be reached
        return True
    
    def re_login(self, log_func=None) -> bool:
        """Re-login when session expires - KEEPS TRYING UNTIL SUCCESS"""
        if log_func:
            log_func("🔄 **Session expired! Re-logging in...**")
        
        # Try to re-login with stored credentials - INFINITE RETRIES
        if hasattr(self, 'login_email') and hasattr(self, 'login_password'):
            return self.auto_login(self.login_email, self.login_password, self.login_callback, max_retries=999999)
        return False

    def select_trim(self, trim_value: str) -> bool:
        try:
            trim_dropdown = self.page.locator('select, [role="combobox"], [role="listbox"]').filter(
                has_text=re.compile(r'trim|TrailSport|Touring|Sport|Limited', re.I)).first

            if trim_dropdown.is_visible(timeout=500):
                trim_dropdown.click()
                option = self.page.locator(f'text="{trim_value}"').first
                if option.is_visible(timeout=300):
                    option.click()
                    return True
        except:
            pass

        try:
            dropdown = self.page.locator('select').first
            if dropdown.is_visible(timeout=300):
                dropdown.select_option(label=trim_value)
                return True
        except:
            pass

        return False

    def scroll_to_export_calculator(self):
        """Scroll down to make Export calculator section visible"""
        try:
            export_calc = self.page.get_by_text("Export calculator", exact=True)
            if export_calc.is_visible(timeout=500):
                export_calc.scroll_into_view_if_needed()
                return True
        except:
            pass
        
        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        return True

    def extract_export_value(self, log_func=None) -> str:
        """
        Extract the 'Export value' from Signal.vin Flutter Web app.
        Export value is calculated: (US Wholesale Value * Exchange Rate) - Export Costs
        """
        def log(msg):
            if log_func:
                log_func(msg)

        log("🔍 **Extracting export value...**")
        
        # Quick Flutter load check
        for i in range(2):
            try:
                loading = self.page.locator('text=Loading...').first
                if not loading.is_visible(timeout=200):
                    break
            except:
                pass
            time.sleep(0.2)
        
        time.sleep(0.3)
        
        # Variables to collect for calculation
        exchange_rate = None
        fx_cushion = 0
        export_cost = None
        target_gpu = None
        us_wholesale_value = None
        customs_duty_rate = 0  # Default 0%
        weekly_depreciation_factor = 0
        average_days_in_inventory = 0
        self.vehicle_make = ''
        self.vehicle_model = ''
        self.vehicle_trim = ''
        
        # Method 1: Parse ALL API responses FIRST to collect data
        log(f"📡 **Checking {len(self.captured_responses)} captured API responses...**")
        
        # FIRST PASS: Extract all needed values from APIs
        for resp in self.captured_responses:
            url = resp.get('url', '')
            body = resp.get('body', '')
            
            # Skip non-JSON responses
            if not body or body.startswith('(function') or body.startswith('<!'):
                continue
            
            try:
                data = json.loads(body)
            except:
                continue
            
            # Get customs_duty_rate and vehicle info from decode API
            if 'decode' in url and 'signal.vin' in url:
                log(f"🚗 **Decode API Response:**")
                log(f"```\n{body[:1500]}\n```")
                
                # Extract make, model, trim
                if 'make' in data:
                    self.vehicle_make = data.get('make', '')
                    log(f"🏭 **Make:** {self.vehicle_make}")
                if 'model' in data:
                    self.vehicle_model = data.get('model', '')
                    log(f"🚙 **Model:** {self.vehicle_model}")
                if 'selected_trim' in data and data['selected_trim']:
                    self.vehicle_trim = data.get('selected_trim', '')
                    log(f"✂️ **Trim:** {self.vehicle_trim}")
                elif 'suggested_trim' in data and data['suggested_trim']:
                    self.vehicle_trim = data.get('suggested_trim', '')
                    log(f"✂️ **Trim (suggested):** {self.vehicle_trim}")
                
                if 'customs_duty_rate' in data:
                    duty = data['customs_duty_rate']
                    log(f"🏛️ **Raw customs_duty_rate value:** `{duty}` (type: {type(duty).__name__})")
                    if duty is not None:
                        try:
                            customs_duty_rate = float(duty)
                            log(f"🏛️ **Customs Duty Rate:** {customs_duty_rate * 100:.2f}%")
                        except:
                            log(f"⚠️ Could not convert customs_duty_rate to float")
                else:
                    log(f"⚠️ **customs_duty_rate field NOT in decode response!**")
            
            # Get offer/initial data (exchange rate, costs, depreciation)
            if 'offer/initial' in url:
                # Extract exchange rate
                if 'exchange_rate' in data:
                    er = data['exchange_rate']
                    if isinstance(er, dict) and 'to_currency_rate' in er:
                        exchange_rate = float(er['to_currency_rate'])
                        log(f"💱 **Base Exchange Rate:** {exchange_rate}")
                    elif isinstance(er, (int, float)):
                        exchange_rate = float(er)
                        log(f"💱 **Base Exchange Rate:** {exchange_rate}")
                
                # Extract depreciation factor
                if 'current_weekly_depreciation_factor' in data:
                    weekly_depreciation_factor = float(data['current_weekly_depreciation_factor'])
                    log(f"📉 **Weekly Depreciation Factor:** {weekly_depreciation_factor}%")
                
                # Extract costs from offer_setup
                if 'offer_setup' in data:
                    setup = data['offer_setup']
                    if 'export_cost_amount' in setup:
                        export_cost = float(setup['export_cost_amount'])
                        log(f"💰 **Export Cost (USD):** ${export_cost}")
                    if 'target_gpu_amount' in setup:
                        target_gpu = float(setup['target_gpu_amount'])
                        log(f"💰 **Target GPU (USD):** ${target_gpu}")
                    if 'fx_cushion_amount' in setup:
                        fx_cushion = float(setup['fx_cushion_amount'])
                        log(f"💱 **FX Cushion:** {fx_cushion}")
                    if 'average_days_in_inventory' in setup:
                        average_days_in_inventory = int(setup['average_days_in_inventory'])
                        log(f"📅 **Avg Days in Inventory:** {average_days_in_inventory}")
            
            # Get retail data
            if 'retail' in url and 'export2' in url:
                log(f"🏪 **Retail API Response:**")
                log(f"```\n{body[:1200]}\n```")
                
                if 'retail' in data:
                    retail = data['retail']
                    log(f"📋 **Retail data keys:** {list(retail.keys()) if isinstance(retail, dict) else type(retail)}")
            
            # Get wholesale value trends - THIS HAS THE WHOLESALE VALUE!
            if 'wholesale_value_trends' in url:
                log(f"📈 **Wholesale Trends API:**")
                log(f"```\n{body[:1000]}\n```")
                
                if 'wholesale_value_trends' in data and data['wholesale_value_trends'] is not None:
                    trends_data = data['wholesale_value_trends']
                    
                    # Get predicted_wholesale_value - THIS IS THE KEY!
                    if 'predicted_wholesale_value' in trends_data and trends_data['predicted_wholesale_value'] is not None:
                        pwv = trends_data['predicted_wholesale_value']
                        log(f"🎯 **predicted_wholesale_value:** {pwv}")
                        
                        if isinstance(pwv, dict) and 'amount' in pwv:
                            us_wholesale_value = float(pwv['amount'])
                            log(f"✅ **Found US Wholesale Value: ${us_wholesale_value} USD**")
                        elif isinstance(pwv, (int, float)):
                            us_wholesale_value = float(pwv)
                            log(f"✅ **Found US Wholesale Value: ${us_wholesale_value} USD**")
                    
                    # Fallback to wholesale_history if needed
                    if not us_wholesale_value and 'wholesale_history' in trends_data and trends_data['wholesale_history'] is not None:
                        history = trends_data['wholesale_history']
                        if 'values' in history and history['values'] and len(history['values']) > 0:
                            latest = history['values'][0]
                            if 'amount' in latest:
                                us_wholesale_value = float(latest['amount'])
                                log(f"✅ **Found US Wholesale from history: ${us_wholesale_value} USD**")
                else:
                    log(f"⚠️ **wholesale_value_trends is NULL - No market data for this vehicle!**")

        # Method 2: Calculate Export Value if we have the data
        if us_wholesale_value and exchange_rate:
            log(f"\n📊 **CALCULATING EXPORT VALUE:**")
            log(f"  - US Wholesale Value: ${us_wholesale_value}")
            log(f"  - Base Exchange Rate: {exchange_rate}")
            log(f"  - FX Cushion: {fx_cushion}")
            
            # Calculate effective FX rate
            effective_fx = exchange_rate - fx_cushion
            log(f"  - Effective FX Rate: {effective_fx}")
            
            log(f"  - Export Cost (USD): ${export_cost or 0}")
            log(f"  - Target GPU (USD): ${target_gpu or 0}")
            log(f"  - Customs Duty Rate: {customs_duty_rate * 100:.2f}%")
            
            # Calculate customs duty
            customs_duty = us_wholesale_value * customs_duty_rate
            log(f"  - Customs Duty (USD): ${customs_duty:.2f}")
            
            # Calculate depreciation
            log(f"  - Weekly Depreciation Factor: {weekly_depreciation_factor}")
            log(f"  - Avg Days in Inventory: {average_days_in_inventory}")
            
            weeks = average_days_in_inventory / 7 if average_days_in_inventory > 0 else 0
            # weekly_depreciation_factor is like 0.1523918 meaning 0.15% per week
            depreciation_rate = weekly_depreciation_factor / 100 if weekly_depreciation_factor > 0 else 0
            depreciation_usd = us_wholesale_value * depreciation_rate * weeks
            log(f"  - Depreciation ({weeks:.2f} weeks): ${depreciation_usd:.2f}")
            
            # Net Value in USD = Wholesale - Export Cost - GPU - Customs Duty - Depreciation
            net_usd = us_wholesale_value - (export_cost or 0) - (target_gpu or 0) - customs_duty - depreciation_usd
            log(f"  - Net Value (USD): ${net_usd:.2f}")
            
            # Export Value in CAD
            export_value_cad = net_usd * effective_fx
            export_value_cad = int(round(export_value_cad))
            
            log(f"✅ **Calculated Export Value: ${export_value_cad} CAD**")
            return str(export_value_cad)
        else:
            log(f"\n⚠️ **Missing data for calculation:**")
            log(f"  - US Wholesale Value: {us_wholesale_value or 'NOT FOUND - No market data available!'}")
            log(f"  - Exchange Rate: {exchange_rate or 'NOT FOUND'}")
            log(f"  - Customs Duty Rate: {customs_duty_rate * 100:.2f}%")
            if not us_wholesale_value:
                log(f"\n💡 **Note:** This vehicle may be too new (2026 model) or rare to have wholesale market data.")
                log(f"   Signal.vin shows 'No data' for Market guide in such cases.")

        # Method 3: Search relevant APIs for direct export value
        log("🔍 **Searching relevant API responses for direct export value...**")
        
        skip_endpoints = ['ceo', 'search/appraisals', 'intercom', 'sentry', 'ping', 'dashboard', 'recalls', 'carfax', 'auth/user']
        
        for resp in self.captured_responses:
            url = resp.get('url', '')
            body = resp.get('body', '')
            
            if any(skip in url.lower() for skip in skip_endpoints):
                continue
            
            if 'export2' not in url and 'offer' not in url:
                continue
            
            patterns = [
                (r'"export_value"[:\s]*([\d.]+)', 'export_value'),
                (r'"exportValue"[:\s]*([\d.]+)', 'exportValue'),
                (r'"appraised_value"[:\s]*([\d.]+)', 'appraised_value'),
                (r'"wholesale_value"[:\s]*([\d.]+)', 'wholesale_value'),
                (r'"market_value"[:\s]*([\d.]+)', 'market_value'),
                (r'"mmr_value"[:\s]*([\d.]+)', 'mmr_value'),
            ]
            
            for pattern, name in patterns:
                m = re.search(pattern, body, re.I)
                if m:
                    val = m.group(1)
                    try:
                        val_int = str(int(float(val)))
                        if len(val_int) >= 4:
                            log(f"✅ **Found {name}: {val_int}**")
                            return val_int
                    except:
                        pass

        # Method 4: Try accessibility tree
        log("🔍 **Checking accessibility tree...**")
        try:
            aria_elements = self.page.locator('[aria-label]').all()
            log(f"  Found {len(aria_elements)} ARIA elements")
            
            for elem in aria_elements[:20]:
                try:
                    label = elem.get_attribute('aria-label')
                    if label and ('$' in label or 'CAD' in label or any(c.isdigit() for c in label)):
                        log(f"  - ARIA: `{label}`")
                        m = re.search(r'\$?\s*([\d,]+)', label)
                        if m:
                            val = m.group(1).replace(",", "")
                            if len(val) >= 4 and val.isdigit():
                                return val
                except:
                    continue
        except Exception as e:
            log(f"⚠️ ARIA error: {e}")

        log("❌ **All extraction methods failed**")
        log("💡 **Note:** Export value might need US wholesale value input from user")
        return None

    def appraise_vehicle(self, vin: str, odometer: str, trim: str = None, list_price: float = 0, listing_url: str = '', carfax_link: str = '', make: str = '', model: str = '', year: str = '', log_func=None, max_retries: int = 999999) -> dict:
        """
        Appraise a single vehicle on Signal.vin
        listing_url, carfax_link, make, model, year come from Google Sheet
        trim comes from Signal.vin
        INFINITE RETRY LOGIC - Never stops on error
        """
        def log(msg):
            if log_func:
                log_func(msg)

        result = {
            'vin': vin,
            'odometer': odometer,
            'trim': trim,
            'list_price': list_price,
            'listing_url': listing_url,
            'carfax_link': carfax_link,
            'make': make,           # from Google Sheet
            'model': model,         # from Google Sheet
            'year': year,           # from Google Sheet
            'signal_trim': '',      # from Signal.vin
            'market_guide_usd': None,
            'export_value_cad': None,
            'profit': None,
            'status': 'PENDING',
            'error': None
        }

        attempt = 0
        while True:  # INFINITE LOOP - keep trying until success
            attempt += 1
            try:
                log(f"🔄 **Attempt {attempt} for VIN: {vin}**")
                
                # Clear previous responses
                self.captured_responses = []
                
                url = f"{self.signal_url}/appraisal/calculate-export?vin={vin}&odometer={odometer}&is-km=true"
                log(f"🌐 **Navigating to:** `{url}`")
                
                # Navigate to URL - wait for full load
                self.page.goto(url, wait_until='domcontentloaded')
                log("⏳ **Waiting for page to load...**")
                time.sleep(1.5)  # Reduced for server
                
                # Check current URL
                current_url = self.page.url
                log(f"📍 **Current URL after navigation:** `{current_url}`")
                
                # Log captured API responses
                log(f"📡 **Captured {len(self.captured_responses)} API responses during load**")
                
                # Check page title
                try:
                    title = self.page.title()
                    log(f"📄 **Page title:** `{title}`")
                except:
                    pass
                
                # Check for login redirect - AUTO RE-LOGIN
                if 'login' in current_url.lower():
                    log("⚠️ **Session expired! Attempting auto re-login...**")
                    if self.re_login(log_func=log):
                        log("✅ **Re-login successful! Retrying VIN...**")
                        continue  # Retry this VIN
                    else:
                        log("❌ **Re-login failed! Skipping this VIN...**")
                        result['status'] = 'SESSION_EXPIRED'
                        result['error'] = 'Re-login failed'
                        return result

                # Scroll to trigger lazy loading and API calls
                log("📜 **Scrolling to load data...**")
                self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.5)  # Reduced
                self.page.evaluate("window.scrollTo(0, 0)")
                time.sleep(0.3)  # Reduced
                self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1)  # Reduced
                
                log(f"📡 **Total captured API responses: {len(self.captured_responses)}**")

                if trim:
                    log(f"🔄 **Selecting trim:** {trim}")
                    self.select_trim(trim)
                    time.sleep(0.5)  # Reduced

                # Wait for export value to fully load
                log(f"⏳ **Waiting for export value to load...**")
                time.sleep(1)  # Reduced

                # Extract value - with retry
                log(f"🔄 **Extracting export value...**")
                export_value = self.extract_export_value(log_func=log_func)
                
                if not export_value:
                    # Retry with additional wait
                    log(f"⏳ **Retrying extraction...**")
                    time.sleep(1)  # Reduced
                    self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(0.5)  # Reduced
                    export_value = self.extract_export_value(log_func=log_func)
                
                if export_value:
                    log(f"✅ **SUCCESS: Export value = ${export_value} CAD**")

                if export_value:
                    result['export_value_cad'] = export_value
                    export_num = float(export_value)
                    if export_num > 0 and list_price > 0:
                        result['profit'] = export_num - list_price
                        result['status'] = 'PROFIT' if result['profit'] > 0 else 'LOSS'
                    else:
                        result['status'] = 'NO PRICE' if list_price == 0 else 'SUCCESS'
                else:
                    result['status'] = 'NO DATA'
                    result['error'] = 'Could not extract export value'
                
                # Add trim from Signal.vin (make and model already set from inventory)
                result['signal_trim'] = self.vehicle_trim
                
                # SUCCESS - break out of retry loop
                return result

            except Exception as e:
                log(f"❌ **ERROR: {e}**")
                result['error'] = str(e)
                result['status'] = 'ERROR'
                
                time.sleep(0.5)
                
                # Quick browser recovery
                try:
                    self.page.goto(self.signal_url)
                except:
                    log("🔄 **Browser recovery...**")
                    try:
                        self.stop()
                        self.start()
                        self.re_login(log_func=log)
                    except Exception as recovery_error:
                        log(f"❌ **Recovery failed: {recovery_error}**")
                        time.sleep(1)
                
                continue  # ALWAYS continue trying - NEVER STOPimport requests
import json
import time
import re
import os
from playwright.sync_api import sync_playwright

# ============================================
# HELPER FUNCTIONS
# ============================================

def is_valid_vin_for_export(vin: str, valid_prefixes: tuple) -> bool:
    if not vin or len(vin) < 17:
        return False
    return vin.strip().upper().startswith(valid_prefixes)

def parse_price(price_str: str) -> float:
    if not price_str:
        return 0.0
    try:
        cleaned = re.sub(r'[^\d.-]', '', str(price_str))
        return float(cleaned) if cleaned else 0.0
    except:
        return 0.0

def format_currency(amount: float) -> str:
    if amount >= 0:
        return f"${amount:,.0f}"
    else:
        return f"-${abs(amount):,.0f}"

def get_supabase_headers(api_key: str):
    return {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

# ============================================
# SUPABASE OPERATIONS
# ============================================

def fetch_sheet_data(supabase_url: str, api_key: str, table_name: str) -> list:
    all_data = []
    batch_size = 1000
    offset = 0

    try:
        while True:
            url = f"{supabase_url}/rest/v1/{table_name}?select=*&limit={batch_size}&offset={offset}"
            response = requests.get(url, headers=get_supabase_headers(api_key), timeout=60)
            response.raise_for_status()
            batch = response.json()

            if not batch:
                break

            all_data.extend(batch)

            if len(batch) < batch_size:
                break

            offset += batch_size

        return all_data
    except Exception as e:
        raise Exception(f"Error fetching data from Supabase: {e}")

def update_sheet_row(supabase_url: str, api_key: str, table_name: str, vin_column: str, export_column: str, vin: str, export_value: str) -> bool:
    try:
        url = f"{supabase_url}/rest/v1/{table_name}?{vin_column}=eq.{vin}"
        payload = {export_column: export_value}
        response = requests.patch(url, json=payload, headers=get_supabase_headers(api_key), timeout=30)
        return response.status_code in [200, 204]
    except:
        return False

def save_to_appraisal_results(supabase_url: str, api_key: str, result: dict, log_func=None) -> bool:
    """Save appraisal result to appraisal_results table"""
    try:
        url = f"{supabase_url}/rest/v1/appraisal_results"

        # Handle export_value - convert string to float properly
        export_val = None
        if result.get('export_value_cad'):
            try:
                clean_val = str(result['export_value_cad']).replace(',', '').replace('$', '').strip()
                export_val = float(clean_val)
            except:
                export_val = None

        # Get listing_url value - DIRECTLY from result
        listing_url_val = result.get('listing_url') or ''

        # Handle price
        price_val = result.get('list_price', 0)
        if isinstance(price_val, str):
            price_val = parse_price(price_val)

        # Handle profit
        profit_val = result.get('profit')
        if profit_val is not None:
            try:
                profit_val = float(profit_val)
            except:
                profit_val = None

        # Get carfax_link value
        carfax_link_val = result.get('carfax_link') or ''
        
        # Get make, model from inventory, trim from Signal.vin
        make_val = result.get('make') or ''      # from inventory
        model_val = result.get('model') or ''    # from inventory
        trim_val = result.get('signal_trim') or ''  # from Signal.vin

        payload = {
            "vin": result.get('vin', ''),
            "kilometers": str(result.get('odometer', '')),
            "listing_link": listing_url_val,   # comes from inventory
            "carfax_link": carfax_link_val,    # comes from inventory
            "make": make_val,                  # comes from inventory
            "model": model_val,                # comes from inventory
            "trim": trim_val,                  # comes from Signal.vin
            "price": price_val,
            "export_value": export_val,        # comes from Signal.vin (Export value)
            "profit": profit_val,
            "status": result.get('status', '')
        }

        # Debug output
        if log_func:
            log_func(f"💾 **Saving to DB:** VIN={payload['vin']}")
            log_func(f"   - make: `{make_val}` (from inventory)")
            log_func(f"   - model: `{model_val}` (from inventory)")
            log_func(f"   - trim: `{trim_val}` (from Signal.vin)")
            log_func(f"   - listing_link: `{listing_url_val}`")
            log_func(f"   - carfax_link: `{carfax_link_val}`")
            log_func(f"   - export_value: `{export_val}`")
            log_func(f"   - price: `{price_val}`")
            log_func(f"   - profit: `{profit_val}`")

        response = requests.post(url, json=payload, headers=get_supabase_headers(api_key), timeout=30)

        if response.status_code not in [200, 201]:
            error_msg = f"Save failed for {result.get('vin')}: {response.status_code} - {response.text}"
            if log_func:
                log_func(f"⚠️ {error_msg}")
            return False

        if log_func:
            log_func(f"✅ Saved to appraisal_results!")
        return True

    except Exception as e:
        if log_func:
            log_func(f"⚠️ Save error: {e}")
        return False

# ============================================
# BROWSER AUTOMATION CLASS
# ============================================

class SignalVinAutomation:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.browser = None
        self.page = None
        self.context = None
        self.playwright = None
        self.logged_in = False
        self.signal_url = "https://app.signal.vin"
        self.captured_responses = []  # Store API responses
        self.vehicle_make = ''
        self.vehicle_model = ''
        self.vehicle_trim = ''

    def start(self):
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(headless=self.headless, slow_mo=0)
        self.context = self.browser.new_context(
            viewport={'width': 1520, 'height': 960},
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            has_touch=True  # Enable touch for checkbox clicking
        )
        self.page = self.context.new_page()
        
        # Set up network response listener
        self.page.on("response", self._capture_response)
        
        return True

    def _capture_response(self, response):
        """Capture API responses that might contain export value"""
        try:
            url = response.url
            # Capture all signal.vin API responses
            if 'signal.vin' in url or 'export' in url.lower():
                try:
                    body = response.text()
                    self.captured_responses.append({
                        'url': url,
                        'status': response.status,
                        'body': body if body else ''  # Full body
                    })
                except:
                    pass
        except:
            pass

    def stop(self):
        try:
            if self.browser:
                self.browser.close()
        except:
            pass
        try:
            if self.playwright:
                self.playwright.stop()
        except:
            pass

    def click_checkbox(self, status_callback=None) -> bool:
        """
        Click the 'I agree' checkbox on Signal.vin login page using multiple methods
        Returns True if checkbox was successfully checked
        """
        def log(msg):
            if status_callback:
                status_callback("info", msg)
        
        log("🔲 Attempting to click checkbox...")
        
        # Get page dimensions
        try:
            box = self.page.locator('flt-glass-pane').first.bounding_box()
            log(f"   Glass pane: {box['width']:.0f} x {box['height']:.0f}")
        except:
            box = {'x': 0, 'y': 0, 'width': 1280, 'height': 800}
        
        # Find checkbox element in semantics
        checkbox_elem = None
        checkbox_box = None
        
        try:
            semantics = self.page.locator('flt-semantics').all()
            log(f"   Found {len(semantics)} semantic elements")
            
            for i, elem in enumerate(semantics):
                try:
                    role = elem.get_attribute('role') or ''
                    label = elem.get_attribute('aria-label') or ''
                    checked = elem.get_attribute('aria-checked') or ''
                    
                    if role == 'checkbox':
                        checkbox_elem = elem
                        checkbox_box = elem.bounding_box()
                        log(f"   🎯 Found checkbox element! checked='{checked}'")
                        break
                except:
                    continue
        except Exception as e:
            log(f"   ⚠️ Error finding semantics: {e}")
        
        # Calculate click position
        if checkbox_box:
            cx = checkbox_box['x'] + checkbox_box['width'] / 2
            cy = checkbox_box['y'] + checkbox_box['height'] / 2
            log(f"   Using detected checkbox position: ({cx:.0f}, {cy:.0f})")
        else:
            # Estimate based on typical Flutter layout
            cx = box['x'] + box['width'] * 0.41  # Slightly left of center
            cy = box['y'] + box['height'] * 0.58  # Below password field
            log(f"   Using estimated position: ({cx:.0f}, {cy:.0f})")
        
        methods_tried = []
        
        # METHOD 1: Direct element click (if found)
        if checkbox_elem:
            log("   → Method 1: Direct element click...")
            try:
                checkbox_elem.click(force=True)
                time.sleep(0.5)
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('Direct element', checked == 'true'))
                if checked == 'true':
                    log("   ✅ Direct click worked!")
                    return True
            except Exception as e:
                log(f"   Failed: {e}")
                methods_tried.append(('Direct element', False))
        
        # METHOD 2: CDP click
        log("   → Method 2: CDP click...")
        try:
            cdp = self.context.new_cdp_session(self.page)
            cdp.send('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': int(cx), 'y': int(cy), 'button': 'left', 'clickCount': 1})
            time.sleep(0.05)
            cdp.send('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': int(cx), 'y': int(cy), 'button': 'left', 'clickCount': 1})
            time.sleep(0.5)
            if checkbox_elem:
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('CDP', checked == 'true'))
                if checked == 'true':
                    log("   ✅ CDP click worked!")
                    return True
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('CDP', False))
        
        # METHOD 3: JavaScript pointer events
        log("   → Method 3: JavaScript pointer events...")
        try:
            self.page.evaluate(f'''() => {{
                const target = document.querySelector('flt-glass-pane');
                const rect = target.getBoundingClientRect();
                
                const evt = new PointerEvent('pointerdown', {{
                    bubbles: true,
                    cancelable: true,
                    view: window,
                    clientX: {cx},
                    clientY: {cy},
                    pointerId: 1,
                    pointerType: 'mouse',
                    isPrimary: true,
                    button: 0,
                    buttons: 1
                }});
                target.dispatchEvent(evt);
                
                setTimeout(() => {{
                    const upEvt = new PointerEvent('pointerup', {{
                        bubbles: true,
                        cancelable: true,
                        view: window,
                        clientX: {cx},
                        clientY: {cy},
                        pointerId: 1,
                        pointerType: 'mouse',
                        isPrimary: true,
                        button: 0,
                        buttons: 0
                    }});
                    target.dispatchEvent(upEvt);
                }}, 30);
            }}''')
            time.sleep(0.3)
            if checkbox_elem:
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('JS Events', checked == 'true'))
                if checked == 'true':
                    log("   ✅ JS pointer events worked!")
                    return True
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('JS Events', False))
        
        # METHOD 4: Playwright mouse click
        log("   → Method 4: Playwright mouse click...")
        try:
            self.page.mouse.click(cx, cy)
            time.sleep(0.3)
            if checkbox_elem:
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('Mouse', checked == 'true'))
                if checked == 'true':
                    log("   ✅ Mouse click worked!")
                    return True
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('Mouse', False))
        
        # METHOD 5: Touch tap
        log("   → Method 5: Touch tap...")
        try:
            self.page.touchscreen.tap(cx, cy)
            time.sleep(0.3)
            if checkbox_elem:
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('Touch', checked == 'true'))
                if checked == 'true':
                    log("   ✅ Touch tap worked!")
                    return True
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('Touch', False))
        
        # METHOD 6: Keyboard Tab + Space
        log("   → Method 6: Keyboard Tab + Space...")
        try:
            self.page.keyboard.press('Tab')
            time.sleep(0.1)
            self.page.keyboard.press('Space')
            time.sleep(0.3)
            if checkbox_elem:
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('Tab+Space', checked == 'true'))
                if checked == 'true':
                    log("   ✅ Tab+Space worked!")
                    return True
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('Tab+Space', False))
        
        # METHOD 7: Click on "I agree" text area
        log("   → Method 7: Click on 'I agree' text area...")
        try:
            text_x = cx + 100
            text_y = cy
            self.page.mouse.click(text_x, text_y)
            time.sleep(0.3)
            if checkbox_elem:
                checked = checkbox_elem.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('Text click', checked == 'true'))
                if checked == 'true':
                    log("   ✅ Text click worked!")
                    return True
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('Text click', False))
        
        # METHOD 8: get_by_role checkbox
        log("   → Method 8: get_by_role('checkbox')...")
        try:
            cb = self.page.get_by_role('checkbox')
            if cb.count() > 0:
                cb.first.check(force=True)
                time.sleep(0.3)
                checked = cb.first.get_attribute('aria-checked')
                log(f"   Result: aria-checked = '{checked}'")
                methods_tried.append(('get_by_role', checked == 'true'))
                if checked == 'true':
                    log("   ✅ get_by_role worked!")
                    return True
            else:
                log("   No checkbox found via get_by_role")
                methods_tried.append(('get_by_role', False))
        except Exception as e:
            log(f"   Failed: {e}")
            methods_tried.append(('get_by_role', False))
        
        # Final verification
        log("🔍 Final checkbox state check...")
        if checkbox_elem:
            final_checked = checkbox_elem.get_attribute('aria-checked')
            log(f"   aria-checked = '{final_checked}'")
            if final_checked == 'true':
                log("✅ CHECKBOX IS CHECKED!")
                return True
            else:
                log("❌ Checkbox still unchecked after all methods")
        
        log("⚠️ All checkbox click methods attempted")
        return False

    def click_login_button(self, status_callback=None) -> bool:
        """
        Click the Login button on Signal.vin login page
        Returns True if login button was clicked
        """
        def log(msg):
            if status_callback:
                status_callback("info", msg)
        
        log("🔘 Attempting to click Login button...")
        
        # Find Login button in semantics
        login_elem = None
        login_box = None
        
        try:
            semantics = self.page.locator('flt-semantics').all()
            
            for elem in semantics:
                try:
                    role = elem.get_attribute('role') or ''
                    label = elem.get_attribute('aria-label') or ''
                    
                    if role == 'button' and 'login' in label.lower():
                        login_elem = elem
                        login_box = elem.bounding_box()
                        log(f"   🎯 Found Login button!")
                        break
                except:
                    continue
        except Exception as e:
            log(f"   ⚠️ Error finding Login button: {e}")
        
        # METHOD 1: Direct element click (if found)
        if login_elem:
            try:
                login_elem.click(force=True)
                time.sleep(0.5)
                log("   ✅ Login button clicked (direct)!")
                return True
            except Exception as e:
                log(f"   ⚠️ Direct click failed: {e}")
        
        # METHOD 2: Click by position
        if login_box:
            try:
                cx = login_box['x'] + login_box['width'] / 2
                cy = login_box['y'] + login_box['height'] / 2
                self.page.mouse.click(cx, cy)
                time.sleep(0.5)
                log("   ✅ Login button clicked (by position)!")
                return True
            except Exception as e:
                log(f"   ⚠️ Position click failed: {e}")
        
        # METHOD 3: Try get_by_role
        try:
            btn = self.page.get_by_role('button', name=re.compile(r'login', re.I))
            if btn.count() > 0:
                btn.first.click(force=True)
                time.sleep(0.5)
                log("   ✅ Login button clicked (get_by_role)!")
                return True
        except Exception as e:
            log(f"   ⚠️ get_by_role failed: {e}")
        
        # METHOD 4: Keyboard - Tab to Login button and press Enter
        log("   → Method 4: Keyboard Tab + Enter...")
        try:
            self.page.keyboard.press('Tab')
            time.sleep(0.2)
            self.page.keyboard.press('Enter')
            time.sleep(0.5)
            log("   ✅ Login button clicked (Tab + Enter)!")
            return True
        except Exception as e:
            log(f"   ⚠️ Keyboard failed: {e}")
        
        # METHOD 5: Click at estimated position (below checkbox)
        log("   → Method 5: Estimated position click...")
        try:
            glass_pane = self.page.locator('flt-glass-pane').first
            box = glass_pane.bounding_box()
            if box:
                # Login button is usually at center-bottom area
                btn_x = box['x'] + box['width'] * 0.5
                btn_y = box['y'] + box['height'] * 0.72
                self.page.mouse.click(btn_x, btn_y)
                time.sleep(0.5)
                log(f"   ✅ Login clicked at estimated position ({btn_x:.0f}, {btn_y:.0f})!")
                return True
        except Exception as e:
            log(f"   ⚠️ Estimated click failed: {e}")
        
        # METHOD 6: CDP click at login button area
        log("   → Method 6: CDP click...")
        try:
            if login_box:
                cx = login_box['x'] + login_box['width'] / 2
                cy = login_box['y'] + login_box['height'] / 2
            else:
                # Estimate position
                glass_pane = self.page.locator('flt-glass-pane').first
                box = glass_pane.bounding_box()
                cx = box['x'] + box['width'] * 0.5
                cy = box['y'] + box['height'] * 0.72
            
            cdp = self.context.new_cdp_session(self.page)
            cdp.send('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': int(cx), 'y': int(cy), 'button': 'left', 'clickCount': 1})
            time.sleep(0.05)
            cdp.send('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': int(cx), 'y': int(cy), 'button': 'left', 'clickCount': 1})
            time.sleep(0.5)
            log("   ✅ Login clicked via CDP!")
            return True
        except Exception as e:
            log(f"   ⚠️ CDP click failed: {e}")
        
        log("⚠️ Could not click Login button - all methods tried")
        return False

    def select_export_product(self, status_callback=None) -> bool:
        """
        Select 'Export' product from the product selection dialog after login
        Step 1: Click Export radio
        Step 2: Click Continue button
        Uses KEYBOARD navigation
        """
        def log(msg):
            if status_callback:
                status_callback("info", msg)
        
        log("📦 Checking for product selection dialog...")
        time.sleep(3)  # Wait for dialog to appear
        
        # Check if dialog is visible
        dialog_found = False
        try:
            semantics = self.page.locator('flt-semantics').all()
            for elem in semantics:
                try:
                    label = elem.get_attribute('aria-label') or ''
                    if 'select product' in label.lower() or 'marketplace' in label.lower():
                        dialog_found = True
                        break
                except:
                    continue
        except:
            pass
        
        if not dialog_found:
            log("   No product selection dialog found - continuing...")
            return True
        
        log("   🎯 Product selection dialog detected!")
        
        # ===== STEP 1: SELECT EXPORT RADIO =====
        log("   📻 Step 1: Clicking on Export...")
        
        # Tab Tab to go directly to Export (skip Marketplace), then Space to click
        try:
            self.page.keyboard.press('Tab')  # Focus on Marketplace
            time.sleep(0.15)
            self.page.keyboard.press('Tab')  # Move to Export
            time.sleep(0.15)
            self.page.keyboard.press('Space')  # Click Export
            time.sleep(0.3)
            log("   ✅ Export clicked! (Tab + Tab + Space)")
        except Exception as e:
            log(f"   ⚠️ Keyboard select failed: {e}")
        
        time.sleep(0.3)
        
        # ===== STEP 2: CLICK CONTINUE BUTTON =====
        log("   🔘 Step 2: Clicking on Continue...")
        
        # Tab to Continue button and press Enter to click it
        try:
            self.page.keyboard.press('Tab')  # Move to Cancel button
            time.sleep(0.1)
            self.page.keyboard.press('Tab')  # Move to Continue button
            time.sleep(0.1)
            self.page.keyboard.press('Enter')  # Click Continue
            time.sleep(0.5)
            log("   ✅ Continue clicked! (Tab + Tab + Enter)")
        except Exception as e:
            log(f"   ⚠️ Method 1 failed: {e}")
        
        # Check if dialog closed
        time.sleep(0.5)
        dialog_still_open = False
        try:
            semantics = self.page.locator('flt-semantics').all()
            for elem in semantics:
                label = elem.get_attribute('aria-label') or ''
                if 'select product' in label.lower() or 'marketplace' in label.lower():
                    dialog_still_open = True
                    break
        except:
            pass
        
        if not dialog_still_open:
            log("✅ Export product selected and Continue clicked!")
            return True
        
        # If dialog still open, try again with different approach
        log("   ⚠️ Dialog still open, trying alternative...")
        
        # Alternative: Tab Tab Space for Export, then Tab Tab Enter for Continue
        try:
            self.page.keyboard.press('Tab')
            time.sleep(0.1)
            self.page.keyboard.press('Tab')  # To Export
            time.sleep(0.1)
            self.page.keyboard.press('Space')  # Select Export
            time.sleep(0.2)
            self.page.keyboard.press('Tab')
            time.sleep(0.1)
            self.page.keyboard.press('Tab')  # To Continue
            time.sleep(0.1)
            self.page.keyboard.press('Enter')  # Click Continue
            time.sleep(0.5)
            log("   ✅ Alternative keyboard done!")
        except Exception as e:
            log(f"   ⚠️ Alternative failed: {e}")
        
        # Final attempt: Direct Tab sequence to Export then Continue
        try:
            self.page.keyboard.press('Tab')
            time.sleep(0.1)
            self.page.keyboard.press('Tab')  # Export
            time.sleep(0.1)
            self.page.keyboard.press('Enter')  # Select Export
            time.sleep(0.2)
            self.page.keyboard.press('Tab')
            time.sleep(0.1)
            self.page.keyboard.press('Tab')
            time.sleep(0.1)
            self.page.keyboard.press('Enter')  # Continue
            time.sleep(0.5)
            log("   ✅ Final keyboard attempt done!")
        except Exception as e:
            log(f"   ⚠️ Final attempt failed: {e}")
        
        log("✅ Product selection completed!")
        return True

    def auto_login(self, email: str, password: str, status_callback=None, max_retries: int = 999999) -> bool:
        """Login with INFINITE retry logic - NEVER STOPS"""
        self.login_email = email
        self.login_password = password
        self.login_callback = status_callback
        
        attempt = 0
        while True:  # INFINITE LOOP - never stop trying
            attempt += 1
            try:
                if status_callback:
                    status_callback("info", f"🔐 Logging in to Signal.vin... (Attempt {attempt})")
                self.page.goto(self.signal_url, wait_until='domcontentloaded')
                time.sleep(0.3)

                if "dashboard" in self.page.url or "appraisal" in self.page.url:
                    if status_callback:
                        status_callback("success", "✅ Already logged in!")
                    self.logged_in = True
                    return True

                try:
                    login_btn = self.page.locator('a:has-text("Login"), button:has-text("Login")').first
                    if login_btn.is_visible():
                        login_btn.click()
                        time.sleep(0.3)
                except:
                    pass

                # Navigate to login page if not there
                if 'login' not in self.page.url.lower():
                    self.page.goto(f"{self.signal_url}/login", wait_until='networkidle')
                    time.sleep(0.5)
                
                # Wait for Flutter to load
                if status_callback:
                    status_callback("info", "⏳ Waiting for Flutter to load...")
                time.sleep(4)

                # ===== CLICK ON EMAIL FIELD FIRST =====
                if status_callback:
                    status_callback("info", "📧 Clicking on email field...")
                
                email_clicked = False
                
                # Method 1: Find textbox in flt-semantics
                try:
                    semantics = self.page.locator('flt-semantics').all()
                    for elem in semantics:
                        role = elem.get_attribute('role') or ''
                        if role == 'textbox':
                            elem.click(force=True)
                            email_clicked = True
                            if status_callback:
                                status_callback("info", "   ✅ Email field clicked (flt-semantics)")
                            break
                except:
                    pass
                
                # Method 2: Find by role textbox
                if not email_clicked:
                    try:
                        textboxes = self.page.get_by_role('textbox').all()
                        if len(textboxes) > 0:
                            textboxes[0].click(force=True)
                            email_clicked = True
                            if status_callback:
                                status_callback("info", "   ✅ Email field clicked (get_by_role)")
                    except:
                        pass
                
                # Method 3: Click on estimated position for email field
                if not email_clicked:
                    try:
                        glass_pane = self.page.locator('flt-glass-pane').first
                        box = glass_pane.bounding_box()
                        if box:
                            email_x = box['x'] + box['width'] * 0.5
                            email_y = box['y'] + box['height'] * 0.35
                            self.page.mouse.click(email_x, email_y)
                            email_clicked = True
                            if status_callback:
                                status_callback("info", f"   ✅ Email field clicked (position: {email_x:.0f}, {email_y:.0f})")
                    except:
                        pass
                
                time.sleep(0.2)

                # ===== TYPE EMAIL =====
                if status_callback:
                    status_callback("info", f"📧 Typing email: {email}")
                self.page.keyboard.type(email, delay=15)
                time.sleep(0.2)

                # ===== TYPE PASSWORD =====
                if status_callback:
                    status_callback("info", "🔑 Filling password...")
                self.page.keyboard.press('Tab')
                time.sleep(0.1)
                self.page.keyboard.type(password, delay=15)
                time.sleep(0.3)

                # ===== CLICK CHECKBOX =====
                if status_callback:
                    status_callback("info", "🔲 Clicking 'I agree' checkbox...")
                checkbox_clicked = self.click_checkbox(status_callback)
                
                if checkbox_clicked:
                    if status_callback:
                        status_callback("success", "✅ Checkbox clicked!")
                    time.sleep(0.5)
                    
                    # ===== CLICK LOGIN BUTTON =====
                    if status_callback:
                        status_callback("info", "🔘 Clicking Login button...")
                    self.click_login_button(status_callback)
                    
                    # ===== SELECT EXPORT PRODUCT =====
                    time.sleep(3)  # Wait for product dialog to appear
                    if status_callback:
                        status_callback("info", "📦 Selecting Export product...")
                    self.select_export_product(status_callback)
                else:
                    if status_callback:
                        status_callback("warning", "👉 Please complete login manually in the browser window (check 'I agree' and click Login)")

                try:
                    self.page.wait_for_url('**/dashboard**', timeout=600000)  # 10 minutes wait
                except:
                    try:
                        self.page.wait_for_url('**/appraisal**', timeout=60000)  # 1 minute wait
                    except:
                        pass

                if "dashboard" in self.page.url or "appraisal" in self.page.url:
                    if status_callback:
                        status_callback("success", "✅ Login successful!")
                    self.logged_in = True
                    return True

            except Exception as e:
                if status_callback:
                    status_callback("warning", f"⚠️ Login attempt {attempt} failed: {e}. Retrying...")
                time.sleep(0.5)
                continue  # NEVER STOP - keep trying
        
        # This should never be reached
        return True
    
    def re_login(self, log_func=None) -> bool:
        """Re-login when session expires - KEEPS TRYING UNTIL SUCCESS"""
        if log_func:
            log_func("🔄 **Session expired! Re-logging in...**")
        
        # Try to re-login with stored credentials - INFINITE RETRIES
        if hasattr(self, 'login_email') and hasattr(self, 'login_password'):
            return self.auto_login(self.login_email, self.login_password, self.login_callback, max_retries=999999)
        return False

    def select_trim(self, trim_value: str) -> bool:
        try:
            trim_dropdown = self.page.locator('select, [role="combobox"], [role="listbox"]').filter(
                has_text=re.compile(r'trim|TrailSport|Touring|Sport|Limited', re.I)).first

            if trim_dropdown.is_visible(timeout=500):
                trim_dropdown.click()
                option = self.page.locator(f'text="{trim_value}"').first
                if option.is_visible(timeout=300):
                    option.click()
                    return True
        except:
            pass

        try:
            dropdown = self.page.locator('select').first
            if dropdown.is_visible(timeout=300):
                dropdown.select_option(label=trim_value)
                return True
        except:
            pass

        return False

    def scroll_to_export_calculator(self):
        """Scroll down to make Export calculator section visible"""
        try:
            export_calc = self.page.get_by_text("Export calculator", exact=True)
            if export_calc.is_visible(timeout=500):
                export_calc.scroll_into_view_if_needed()
                return True
        except:
            pass
        
        self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        return True

    def extract_export_value(self, log_func=None) -> str:
        """
        Extract the 'Export value' from Signal.vin Flutter Web app.
        Export value is calculated: (US Wholesale Value * Exchange Rate) - Export Costs
        """
        def log(msg):
            if log_func:
                log_func(msg)

        log("🔍 **Extracting export value...**")
        
        # Quick Flutter load check
        for i in range(2):
            try:
                loading = self.page.locator('text=Loading...').first
                if not loading.is_visible(timeout=200):
                    break
            except:
                pass
            time.sleep(0.2)
        
        time.sleep(0.3)
        
        # Variables to collect for calculation
        exchange_rate = None
        fx_cushion = 0
        export_cost = None
        target_gpu = None
        us_wholesale_value = None
        customs_duty_rate = 0  # Default 0%
        weekly_depreciation_factor = 0
        average_days_in_inventory = 0
        self.vehicle_make = ''
        self.vehicle_model = ''
        self.vehicle_trim = ''
        
        # Method 1: Parse ALL API responses FIRST to collect data
        log(f"📡 **Checking {len(self.captured_responses)} captured API responses...**")
        
        # FIRST PASS: Extract all needed values from APIs
        for resp in self.captured_responses:
            url = resp.get('url', '')
            body = resp.get('body', '')
            
            # Skip non-JSON responses
            if not body or body.startswith('(function') or body.startswith('<!'):
                continue
            
            try:
                data = json.loads(body)
            except:
                continue
            
            # Get customs_duty_rate and vehicle info from decode API
            if 'decode' in url and 'signal.vin' in url:
                log(f"🚗 **Decode API Response:**")
                log(f"```\n{body[:1500]}\n```")
                
                # Extract make, model, trim
                if 'make' in data:
                    self.vehicle_make = data.get('make', '')
                    log(f"🏭 **Make:** {self.vehicle_make}")
                if 'model' in data:
                    self.vehicle_model = data.get('model', '')
                    log(f"🚙 **Model:** {self.vehicle_model}")
                if 'selected_trim' in data and data['selected_trim']:
                    self.vehicle_trim = data.get('selected_trim', '')
                    log(f"✂️ **Trim:** {self.vehicle_trim}")
                elif 'suggested_trim' in data and data['suggested_trim']:
                    self.vehicle_trim = data.get('suggested_trim', '')
                    log(f"✂️ **Trim (suggested):** {self.vehicle_trim}")
                
                if 'customs_duty_rate' in data:
                    duty = data['customs_duty_rate']
                    log(f"🏛️ **Raw customs_duty_rate value:** `{duty}` (type: {type(duty).__name__})")
                    if duty is not None:
                        try:
                            customs_duty_rate = float(duty)
                            log(f"🏛️ **Customs Duty Rate:** {customs_duty_rate * 100:.2f}%")
                        except:
                            log(f"⚠️ Could not convert customs_duty_rate to float")
                else:
                    log(f"⚠️ **customs_duty_rate field NOT in decode response!**")
            
            # Get offer/initial data (exchange rate, costs, depreciation)
            if 'offer/initial' in url:
                # Extract exchange rate
                if 'exchange_rate' in data:
                    er = data['exchange_rate']
                    if isinstance(er, dict) and 'to_currency_rate' in er:
                        exchange_rate = float(er['to_currency_rate'])
                        log(f"💱 **Base Exchange Rate:** {exchange_rate}")
                    elif isinstance(er, (int, float)):
                        exchange_rate = float(er)
                        log(f"💱 **Base Exchange Rate:** {exchange_rate}")
                
                # Extract depreciation factor
                if 'current_weekly_depreciation_factor' in data:
                    weekly_depreciation_factor = float(data['current_weekly_depreciation_factor'])
                    log(f"📉 **Weekly Depreciation Factor:** {weekly_depreciation_factor}%")
                
                # Extract costs from offer_setup
                if 'offer_setup' in data:
                    setup = data['offer_setup']
                    if 'export_cost_amount' in setup:
                        export_cost = float(setup['export_cost_amount'])
                        log(f"💰 **Export Cost (USD):** ${export_cost}")
                    if 'target_gpu_amount' in setup:
                        target_gpu = float(setup['target_gpu_amount'])
                        log(f"💰 **Target GPU (USD):** ${target_gpu}")
                    if 'fx_cushion_amount' in setup:
                        fx_cushion = float(setup['fx_cushion_amount'])
                        log(f"💱 **FX Cushion:** {fx_cushion}")
                    if 'average_days_in_inventory' in setup:
                        average_days_in_inventory = int(setup['average_days_in_inventory'])
                        log(f"📅 **Avg Days in Inventory:** {average_days_in_inventory}")
            
            # Get retail data
            if 'retail' in url and 'export2' in url:
                log(f"🏪 **Retail API Response:**")
                log(f"```\n{body[:1200]}\n```")
                
                if 'retail' in data:
                    retail = data['retail']
                    log(f"📋 **Retail data keys:** {list(retail.keys()) if isinstance(retail, dict) else type(retail)}")
            
            # Get wholesale value trends - THIS HAS THE WHOLESALE VALUE!
            if 'wholesale_value_trends' in url:
                log(f"📈 **Wholesale Trends API:**")
                log(f"```\n{body[:1000]}\n```")
                
                if 'wholesale_value_trends' in data and data['wholesale_value_trends'] is not None:
                    trends_data = data['wholesale_value_trends']
                    
                    # Get predicted_wholesale_value - THIS IS THE KEY!
                    if 'predicted_wholesale_value' in trends_data and trends_data['predicted_wholesale_value'] is not None:
                        pwv = trends_data['predicted_wholesale_value']
                        log(f"🎯 **predicted_wholesale_value:** {pwv}")
                        
                        if isinstance(pwv, dict) and 'amount' in pwv:
                            us_wholesale_value = float(pwv['amount'])
                            log(f"✅ **Found US Wholesale Value: ${us_wholesale_value} USD**")
                        elif isinstance(pwv, (int, float)):
                            us_wholesale_value = float(pwv)
                            log(f"✅ **Found US Wholesale Value: ${us_wholesale_value} USD**")
                    
                    # Fallback to wholesale_history if needed
                    if not us_wholesale_value and 'wholesale_history' in trends_data and trends_data['wholesale_history'] is not None:
                        history = trends_data['wholesale_history']
                        if 'values' in history and history['values'] and len(history['values']) > 0:
                            latest = history['values'][0]
                            if 'amount' in latest:
                                us_wholesale_value = float(latest['amount'])
                                log(f"✅ **Found US Wholesale from history: ${us_wholesale_value} USD**")
                else:
                    log(f"⚠️ **wholesale_value_trends is NULL - No market data for this vehicle!**")

        # Method 2: Calculate Export Value if we have the data
        if us_wholesale_value and exchange_rate:
            log(f"\n📊 **CALCULATING EXPORT VALUE:**")
            log(f"  - US Wholesale Value: ${us_wholesale_value}")
            log(f"  - Base Exchange Rate: {exchange_rate}")
            log(f"  - FX Cushion: {fx_cushion}")
            
            # Calculate effective FX rate
            effective_fx = exchange_rate - fx_cushion
            log(f"  - Effective FX Rate: {effective_fx}")
            
            log(f"  - Export Cost (USD): ${export_cost or 0}")
            log(f"  - Target GPU (USD): ${target_gpu or 0}")
            log(f"  - Customs Duty Rate: {customs_duty_rate * 100:.2f}%")
            
            # Calculate customs duty
            customs_duty = us_wholesale_value * customs_duty_rate
            log(f"  - Customs Duty (USD): ${customs_duty:.2f}")
            
            # Calculate depreciation
            log(f"  - Weekly Depreciation Factor: {weekly_depreciation_factor}")
            log(f"  - Avg Days in Inventory: {average_days_in_inventory}")
            
            weeks = average_days_in_inventory / 7 if average_days_in_inventory > 0 else 0
            # weekly_depreciation_factor is like 0.1523918 meaning 0.15% per week
            depreciation_rate = weekly_depreciation_factor / 100 if weekly_depreciation_factor > 0 else 0
            depreciation_usd = us_wholesale_value * depreciation_rate * weeks
            log(f"  - Depreciation ({weeks:.2f} weeks): ${depreciation_usd:.2f}")
            
            # Net Value in USD = Wholesale - Export Cost - GPU - Customs Duty - Depreciation
            net_usd = us_wholesale_value - (export_cost or 0) - (target_gpu or 0) - customs_duty - depreciation_usd
            log(f"  - Net Value (USD): ${net_usd:.2f}")
            
            # Export Value in CAD
            export_value_cad = net_usd * effective_fx
            export_value_cad = int(round(export_value_cad))
            
            log(f"✅ **Calculated Export Value: ${export_value_cad} CAD**")
            return str(export_value_cad)
        else:
            log(f"\n⚠️ **Missing data for calculation:**")
            log(f"  - US Wholesale Value: {us_wholesale_value or 'NOT FOUND - No market data available!'}")
            log(f"  - Exchange Rate: {exchange_rate or 'NOT FOUND'}")
            log(f"  - Customs Duty Rate: {customs_duty_rate * 100:.2f}%")
            if not us_wholesale_value:
                log(f"\n💡 **Note:** This vehicle may be too new (2026 model) or rare to have wholesale market data.")
                log(f"   Signal.vin shows 'No data' for Market guide in such cases.")

        # Method 3: Search relevant APIs for direct export value
        log("🔍 **Searching relevant API responses for direct export value...**")
        
        skip_endpoints = ['ceo', 'search/appraisals', 'intercom', 'sentry', 'ping', 'dashboard', 'recalls', 'carfax', 'auth/user']
        
        for resp in self.captured_responses:
            url = resp.get('url', '')
            body = resp.get('body', '')
            
            if any(skip in url.lower() for skip in skip_endpoints):
                continue
            
            if 'export2' not in url and 'offer' not in url:
                continue
            
            patterns = [
                (r'"export_value"[:\s]*([\d.]+)', 'export_value'),
                (r'"exportValue"[:\s]*([\d.]+)', 'exportValue'),
                (r'"appraised_value"[:\s]*([\d.]+)', 'appraised_value'),
                (r'"wholesale_value"[:\s]*([\d.]+)', 'wholesale_value'),
                (r'"market_value"[:\s]*([\d.]+)', 'market_value'),
                (r'"mmr_value"[:\s]*([\d.]+)', 'mmr_value'),
            ]
            
            for pattern, name in patterns:
                m = re.search(pattern, body, re.I)
                if m:
                    val = m.group(1)
                    try:
                        val_int = str(int(float(val)))
                        if len(val_int) >= 4:
                            log(f"✅ **Found {name}: {val_int}**")
                            return val_int
                    except:
                        pass

        # Method 4: Try accessibility tree
        log("🔍 **Checking accessibility tree...**")
        try:
            aria_elements = self.page.locator('[aria-label]').all()
            log(f"  Found {len(aria_elements)} ARIA elements")
            
            for elem in aria_elements[:20]:
                try:
                    label = elem.get_attribute('aria-label')
                    if label and ('$' in label or 'CAD' in label or any(c.isdigit() for c in label)):
                        log(f"  - ARIA: `{label}`")
                        m = re.search(r'\$?\s*([\d,]+)', label)
                        if m:
                            val = m.group(1).replace(",", "")
                            if len(val) >= 4 and val.isdigit():
                                return val
                except:
                    continue
        except Exception as e:
            log(f"⚠️ ARIA error: {e}")

        log("❌ **All extraction methods failed**")
        log("💡 **Note:** Export value might need US wholesale value input from user")
        return None

    def appraise_vehicle(self, vin: str, odometer: str, trim: str = None, list_price: float = 0, listing_url: str = '', carfax_link: str = '', make: str = '', model: str = '', year: str = '', log_func=None, max_retries: int = 999999) -> dict:
        """
        Appraise a single vehicle on Signal.vin
        listing_url, carfax_link, make, model, year come from Google Sheet
        trim comes from Signal.vin
        INFINITE RETRY LOGIC - Never stops on error
        """
        def log(msg):
            if log_func:
                log_func(msg)

        result = {
            'vin': vin,
            'odometer': odometer,
            'trim': trim,
            'list_price': list_price,
            'listing_url': listing_url,
            'carfax_link': carfax_link,
            'make': make,           # from Google Sheet
            'model': model,         # from Google Sheet
            'year': year,           # from Google Sheet
            'signal_trim': '',      # from Signal.vin
            'market_guide_usd': None,
            'export_value_cad': None,
            'profit': None,
            'status': 'PENDING',
            'error': None
        }

        attempt = 0
        while True:  # INFINITE LOOP - keep trying until success
            attempt += 1
            try:
                log(f"🔄 **Attempt {attempt} for VIN: {vin}**")
                
                # Clear previous responses
                self.captured_responses = []
                
                url = f"{self.signal_url}/appraisal/calculate-export?vin={vin}&odometer={odometer}&is-km=true"
                log(f"🌐 **Navigating to:** `{url}`")
                
                # Navigate to URL - wait for full load
                self.page.goto(url, wait_until='domcontentloaded')
                log("⏳ **Waiting for page to load...**")
                time.sleep(1.5)  # Reduced for server
                
                # Check current URL
                current_url = self.page.url
                log(f"📍 **Current URL after navigation:** `{current_url}`")
                
                # Log captured API responses
                log(f"📡 **Captured {len(self.captured_responses)} API responses during load**")
                
                # Check page title
                try:
                    title = self.page.title()
                    log(f"📄 **Page title:** `{title}`")
                except:
                    pass
                
                # Check for login redirect - AUTO RE-LOGIN
                if 'login' in current_url.lower():
                    log("⚠️ **Session expired! Attempting auto re-login...**")
                    if self.re_login(log_func=log):
                        log("✅ **Re-login successful! Retrying VIN...**")
                        continue  # Retry this VIN
                    else:
                        log("❌ **Re-login failed! Skipping this VIN...**")
                        result['status'] = 'SESSION_EXPIRED'
                        result['error'] = 'Re-login failed'
                        return result

                # Scroll to trigger lazy loading and API calls
                log("📜 **Scrolling to load data...**")
                self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.5)  # Reduced
                self.page.evaluate("window.scrollTo(0, 0)")
                time.sleep(0.3)  # Reduced
                self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1)  # Reduced
                
                log(f"📡 **Total captured API responses: {len(self.captured_responses)}**")

                if trim:
                    log(f"🔄 **Selecting trim:** {trim}")
                    self.select_trim(trim)
                    time.sleep(0.5)  # Reduced

                # Wait for export value to fully load
                log(f"⏳ **Waiting for export value to load...**")
                time.sleep(1)  # Reduced

                # Extract value - with retry
                log(f"🔄 **Extracting export value...**")
                export_value = self.extract_export_value(log_func=log_func)
                
                if not export_value:
                    # Retry with additional wait
                    log(f"⏳ **Retrying extraction...**")
                    time.sleep(1)  # Reduced
                    self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(0.5)  # Reduced
                    export_value = self.extract_export_value(log_func=log_func)
                
                if export_value:
                    log(f"✅ **SUCCESS: Export value = ${export_value} CAD**")

                if export_value:
                    result['export_value_cad'] = export_value
                    export_num = float(export_value)
                    if export_num > 0 and list_price > 0:
                        result['profit'] = export_num - list_price
                        result['status'] = 'PROFIT' if result['profit'] > 0 else 'LOSS'
                    else:
                        result['status'] = 'NO PRICE' if list_price == 0 else 'SUCCESS'
                else:
                    result['status'] = 'NO DATA'
                    result['error'] = 'Could not extract export value'
                
                # Add trim from Signal.vin (make and model already set from inventory)
                result['signal_trim'] = self.vehicle_trim
                
                # SUCCESS - break out of retry loop
                return result

            except Exception as e:
                log(f"❌ **ERROR: {e}**")
                result['error'] = str(e)
                result['status'] = 'ERROR'
                
                time.sleep(0.5)
                
                # Quick browser recovery
                try:
                    self.page.goto(self.signal_url)
                except:
                    log("🔄 **Browser recovery...**")
                    try:
                        self.stop()
                        self.start()
                        self.re_login(log_func=log)
                    except Exception as recovery_error:
                        log(f"❌ **Recovery failed: {recovery_error}**")
                        time.sleep(1)
                
                continue  # ALWAYS continue trying - NEVER STOP
