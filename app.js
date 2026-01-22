// ============================================
// STATE MANAGEMENT
// ============================================
let sheetData = null;
let validRows = [];
let results = [];
let isProcessing = false;
let eventSource = null;

// ============================================
// UTILITY FUNCTIONS
// ============================================
function formatCurrency(amount) {
    if (amount >= 0) {
        return `$${amount.toLocaleString('en-US', {maximumFractionDigits: 0})}`;
    } else {
        return `-$${Math.abs(amount).toLocaleString('en-US', {maximumFractionDigits: 0})}`;
    }
}

function parsePrice(priceStr) {
    if (!priceStr) return 0;
    const cleaned = String(priceStr).replace(/[^\d.-]/g, '');
    return parseFloat(cleaned) || 0;
}

function getValidPrefixes() {
    const prefixes = [];
    document.querySelectorAll('.prefix-tag.active').forEach(tag => {
        prefixes.push(tag.dataset.prefix);
    });
    return prefixes;
}

function isValidVin(vin, prefixes) {
    if (!vin || vin.length < 17) return false;
    return prefixes.some(p => vin.toUpperCase().startsWith(p));
}

function togglePassword(inputId) {
    const input = document.getElementById(inputId);
    input.type = input.type === 'password' ? 'text' : 'password';
}

function toggleExpander(element) {
    const content = element.nextElementSibling;
    const icon = element.querySelector('.expander-icon');
    
    if (content.classList.contains('collapsed')) {
        content.classList.remove('collapsed');
        icon.textContent = '▼';
    } else {
        content.classList.add('collapsed');
        icon.textContent = '▶';
    }
}

// ============================================
// GOOGLE SHEETS URL PARSER
// ============================================
function parseGoogleSheetUrl(url) {
    // Extract spreadsheet ID from various Google Sheets URL formats
    // Format 1: https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit...
    // Format 2: https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/gviz/tq...
    
    const patterns = [
        /\/spreadsheets\/d\/([a-zA-Z0-9-_]+)/,
        /\/d\/([a-zA-Z0-9-_]+)/
    ];
    
    for (const pattern of patterns) {
        const match = url.match(pattern);
        if (match) {
            return match[1];
        }
    }
    return null;
}

// ============================================
// TAB NAVIGATION
// ============================================
document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        // Remove active class from all tabs
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        
        // Add active class to clicked tab
        tab.classList.add('active');
        document.getElementById(tab.dataset.tab).classList.add('active');
    });
});

// ============================================
// PREFIX TAG TOGGLE
// ============================================
document.querySelectorAll('.prefix-tag').forEach(tag => {
    tag.addEventListener('click', () => {
        tag.classList.toggle('active');
        if (tag.classList.contains('active')) {
            tag.textContent = tag.dataset.prefix + ' ×';
        } else {
            tag.textContent = tag.dataset.prefix;
        }
        
        // Update prefixes display
        document.getElementById('prefixesDisplay').textContent = getValidPrefixes().join(', ') || 'None';
        
        // Re-filter data if loaded
        if (sheetData) {
            filterAndDisplayData();
        }
    });
});

// ============================================
// FETCH DATA FROM GOOGLE SHEETS
// ============================================
async function fetchData() {
    const googleSheetUrl = document.getElementById('googleSheetUrl').value.trim();
    
    if (!googleSheetUrl) {
        alert('Please enter a Google Sheet URL');
        return;
    }
    
    const spreadsheetId = parseGoogleSheetUrl(googleSheetUrl);
    
    if (!spreadsheetId) {
        alert('Invalid Google Sheet URL. Please use a valid Google Sheets link.');
        return;
    }
    
    const loadStatus = document.getElementById('loadStatus');
    loadStatus.classList.remove('hidden', 'success', 'error');
    loadStatus.textContent = '⏳ Fetching data from Google Sheets...';
    loadStatus.classList.add('info-message');
    
    try {
        // Use Google Sheets API v4 with public access (CSV export)
        // This requires the sheet to be publicly accessible
        const csvUrl = `https://docs.google.com/spreadsheets/d/${spreadsheetId}/export?format=csv`;
        
        loadStatus.textContent = '⏳ Downloading sheet data...';
        
        const response = await fetch(csvUrl);
        
        if (!response.ok) {
            throw new Error(`Failed to fetch sheet. Make sure it's publicly accessible (Share > Anyone with link can view)`);
        }
        
        const csvText = await response.text();
        
        // Parse CSV
        loadStatus.textContent = '⏳ Parsing CSV data...';
        sheetData = parseCSV(csvText);
        
        if (sheetData.length === 0) {
            throw new Error('No data found in the sheet');
        }
        
        loadStatus.textContent = `✅ Loaded ${sheetData.length} rows from Google Sheets`;
        loadStatus.classList.remove('info-message');
        loadStatus.classList.add('success-message');
        
        filterAndDisplayData();
        
    } catch (error) {
        loadStatus.textContent = `❌ Error: ${error.message}`;
        loadStatus.classList.remove('info-message');
        loadStatus.classList.add('error-message');
    }
}

// ============================================
// CSV PARSER
// ============================================
function parseCSV(csvText) {
    const lines = csvText.split('\n');
    if (lines.length < 2) return [];
    
    // Parse header row
    const headers = parseCSVLine(lines[0]);
    
    // Parse data rows
    const data = [];
    for (let i = 1; i < lines.length; i++) {
        const line = lines[i].trim();
        if (!line) continue;
        
        const values = parseCSVLine(line);
        const row = {};
        
        headers.forEach((header, index) => {
            row[header.trim().toLowerCase()] = values[index] || '';
        });
        
        data.push(row);
    }
    
    return data;
}

function parseCSVLine(line) {
    const result = [];
    let current = '';
    let inQuotes = false;
    
    for (let i = 0; i < line.length; i++) {
        const char = line[i];
        
        if (char === '"') {
            if (inQuotes && line[i + 1] === '"') {
                current += '"';
                i++;
            } else {
                inQuotes = !inQuotes;
            }
        } else if (char === ',' && !inQuotes) {
            result.push(current);
            current = '';
        } else {
            current += char;
        }
    }
    
    result.push(current);
    return result;
}

// ============================================
// FILTER AND DISPLAY DATA
// ============================================
function filterAndDisplayData() {
    if (!sheetData) return;
    
    const vinColumn = document.getElementById('vinColumn').value.toLowerCase();
    const prefixes = getValidPrefixes();
    
    // Get columns
    const columns = sheetData.length > 0 ? Object.keys(sheetData[0]) : [];
    
    // Filter valid and invalid VINs
    validRows = [];
    const invalidRows = [];
    
    sheetData.forEach(row => {
        const vin = String(row[vinColumn] || '').trim().toUpperCase();
        if (isValidVin(vin, prefixes)) {
            validRows.push(row);
        } else {
            invalidRows.push(row);
        }
    });
    
    // Update metrics
    document.getElementById('metricsSection').classList.remove('hidden');
    document.getElementById('totalRows').textContent = sheetData.length;
    document.getElementById('validVins').textContent = validRows.length;
    document.getElementById('skippedVins').textContent = invalidRows.length;
    document.getElementById('prefixesDisplay').textContent = prefixes.join(', ') || 'None';
    
    // Display valid VINs table
    displayTable('validVinsSection', 'validVinsHeader', 'validVinsBody', validRows, columns);
    
    // Display skipped VINs table
    if (invalidRows.length > 0) {
        document.getElementById('skippedVinsSection').classList.remove('hidden');
        displayTable('skippedVinsSection', 'skippedVinsHeader', 'skippedVinsBody', invalidRows, columns);
    } else {
        document.getElementById('skippedVinsSection').classList.add('hidden');
    }
    
    // Update Process VINs tab
    updateProcessTab();
}

function displayTable(sectionId, headerId, bodyId, data, columns) {
    const section = document.getElementById(sectionId);
    section.classList.remove('hidden');
    
    const header = document.getElementById(headerId);
    const body = document.getElementById(bodyId);
    
    // Build header
    header.innerHTML = columns.map(col => `<th>${col}</th>`).join('');
    
    // Build body (show all rows, table container has scroll)
    body.innerHTML = data.map(row => {
        return `<tr>${columns.map(col => `<td>${row[col] || ''}</td>`).join('')}</tr>`;
    }).join('');
}

// ============================================
// CLEAR DATA
// ============================================
function clearData() {
    sheetData = null;
    validRows = [];
    results = [];
    
    document.getElementById('loadStatus').classList.add('hidden');
    document.getElementById('metricsSection').classList.add('hidden');
    document.getElementById('validVinsSection').classList.add('hidden');
    document.getElementById('skippedVinsSection').classList.add('hidden');
    
    updateProcessTab();
    updateResultsTab();
}

// ============================================
// UPDATE PROCESS TAB
// ============================================
function updateProcessTab() {
    const noDataWarning = document.getElementById('noDataWarning');
    const processSection = document.getElementById('processSection');
    
    if (!sheetData || validRows.length === 0) {
        noDataWarning.classList.remove('hidden');
        processSection.classList.add('hidden');
    } else {
        noDataWarning.classList.add('hidden');
        processSection.classList.remove('hidden');
        
        document.getElementById('readyCount').textContent = validRows.length;
    }
}

// ============================================
// START PROCESSING
// ============================================
async function startProcessing() {
    if (isProcessing) return;
    if (validRows.length === 0) {
        alert('No valid VINs to process!');
        return;
    }
    
    isProcessing = true;
    results = [];
    
    // Get configuration
    const vinColumn = document.getElementById('vinColumn').value.toLowerCase();
    const odometerColumn = document.getElementById('odometerColumn').value.toLowerCase();
    const trimColumn = document.getElementById('trimColumn').value.toLowerCase();
    const priceColumn = document.getElementById('priceColumn').value.toLowerCase();
    const urlColumn = document.getElementById('urlColumn').value.toLowerCase();
    const yearColumn = document.getElementById('yearColumn').value.toLowerCase();
    
    const config = {
        signal_email: document.getElementById('signalEmail').value,
        signal_password: document.getElementById('signalPassword').value,
        headless: document.getElementById('headless').checked,
        valid_rows: validRows.map(row => ({
            vin: String(row[vinColumn] || '').trim().toUpperCase(),
            odometer: String(row[odometerColumn] || '0').trim() || '0',
            trim: String(row[trimColumn] || '').trim(),
            list_price: parsePrice(row[priceColumn]),
            listing_url: String(row[urlColumn] || '').trim(),
            carfax_link: String(row['carfax_link'] || '').trim(),
            make: String(row['make'] || '').trim(),
            model: String(row['model'] || '').trim(),
            year: String(row[yearColumn] || '').trim()
        }))
    };
    
    // Show progress
    document.getElementById('progressSection').classList.remove('hidden');
    document.getElementById('logsSection').classList.remove('hidden');
    document.getElementById('startBtn').disabled = true;
    document.getElementById('runningIndicator').classList.remove('hidden');
    
    const logsContainer = document.getElementById('logsContainer');
    logsContainer.innerHTML = '';
    
    try {
        // Start SSE connection
        const response = await fetch('/api/process', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(config)
        });
        
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        
        while (true) {
            const {value, done} = await reader.read();
            if (done) break;
            
            const text = decoder.decode(value);
            const lines = text.split('\n').filter(line => line.startsWith('data: '));
            
            for (const line of lines) {
                try {
                    const data = JSON.parse(line.substring(6));
                    handleProcessingUpdate(data);
                } catch (e) {
                    console.error('Parse error:', e);
                }
            }
        }
        
    } catch (error) {
        addLog(`❌ Error: ${error.message}`, 'error');
    } finally {
        isProcessing = false;
        document.getElementById('startBtn').disabled = false;
        document.getElementById('runningIndicator').classList.add('hidden');
        updateResultsTab();
    }
}

function handleProcessingUpdate(data) {
    const logsContainer = document.getElementById('logsContainer');
    
    switch (data.type) {
        case 'progress':
            document.getElementById('progressFill').style.width = `${data.progress * 100}%`;
            document.getElementById('progressText').textContent = data.message;
            break;
            
        case 'log':
            addLog(data.message, data.level || 'info');
            break;
            
        case 'result':
            results.push(data.result);
            updateResultsTab();
            break;
            
        case 'complete':
            addLog(`✅ ${data.message}`, 'success');
            document.getElementById('progressFill').style.width = '100%';
            break;
            
        case 'error':
            addLog(`❌ ${data.message}`, 'error');
            break;
    }
}

function addLog(message, level = 'info') {
    const logsContainer = document.getElementById('logsContainer');
    const logEntry = document.createElement('div');
    logEntry.className = `log-entry ${level}`;
    logEntry.innerHTML = message;
    logsContainer.appendChild(logEntry);
    logsContainer.scrollTop = logsContainer.scrollHeight;
}

// ============================================
// STOP PROCESSING
// ============================================
function stopProcessing() {
    if (eventSource) {
        eventSource.close();
    }
    fetch('/api/stop', { method: 'POST' });
    isProcessing = false;
    document.getElementById('startBtn').disabled = false;
    document.getElementById('runningIndicator').classList.add('hidden');
    addLog('⏹️ Processing stopped by user', 'warning');
}

// ============================================
// UPDATE RESULTS TAB
// ============================================
function updateResultsTab() {
    const noResultsMessage = document.getElementById('noResultsMessage');
    const resultsContent = document.getElementById('resultsContent');
    
    if (results.length === 0) {
        noResultsMessage.classList.remove('hidden');
        resultsContent.classList.add('hidden');
        return;
    }
    
    noResultsMessage.classList.add('hidden');
    resultsContent.classList.remove('hidden');
    
    // Calculate stats
    const successful = results.filter(r => r.export_value_cad);
    const errors = results.filter(r => r.status === 'ERROR' || r.status === 'NO DATA');
    
    // Update metrics
    document.getElementById('successCount').textContent = successful.length;
    document.getElementById('errorCount').textContent = errors.length;
    document.getElementById('totalProcessed').textContent = results.length;
    
    // Results table
    const resultsBody = document.getElementById('resultsBody');
    resultsBody.innerHTML = results.map(r => `
        <tr>
            <td>${r.vin}</td>
            <td>${r.odometer}</td>
            <td>${r.export_value_cad ? formatCurrency(parseFloat(r.export_value_cad)) : 'N/A'}</td>
            <td style="color: ${r.export_value_cad ? '#81c784' : '#e57373'};">${r.status}</td>
        </tr>
    `).join('');
    
    // JSON Preview
    const jsonOutput = formatResultsAsJSON();
    document.getElementById('jsonPreview').textContent = JSON.stringify(jsonOutput, null, 2);
}

// ============================================
// FORMAT RESULTS AS JSON
// ============================================
function formatResultsAsJSON() {
    return {
        generated_at: new Date().toISOString(),
        total_processed: results.length,
        successful: results.filter(r => r.export_value_cad).length,
        errors: results.filter(r => !r.export_value_cad).length,
        results: results.map(r => ({
            vin: r.vin,
            make: r.make || '',
            model: r.model || '',
            year: r.year || '',
            trim: r.signal_trim || r.trim || '',
            odometer: r.odometer,
            list_price: r.list_price || 0,
            export_value_cad: r.export_value_cad ? parseFloat(r.export_value_cad) : null,
            profit: r.profit || null,
            listing_url: r.listing_url || '',
            carfax_link: r.carfax_link || '',
            status: r.status
        }))
    };
}

// ============================================
// DOWNLOAD FUNCTIONS
// ============================================
function downloadJSON() {
    if (results.length === 0) {
        alert('No results to download!');
        return;
    }
    
    const jsonOutput = formatResultsAsJSON();
    const json = JSON.stringify(jsonOutput, null, 2);
    downloadFile(json, `signal_vin_results_${getTimestamp()}.json`, 'application/json');
}

function downloadCSV() {
    if (results.length === 0) {
        alert('No results to download!');
        return;
    }
    
    const headers = ['vin', 'make', 'model', 'year', 'trim', 'odometer', 'list_price', 'export_value_cad', 'profit', 'listing_url', 'carfax_link', 'status'];
    const rows = results.map(r => [
        r.vin,
        r.make || '',
        r.model || '',
        r.year || '',
        r.signal_trim || r.trim || '',
        r.odometer,
        r.list_price || 0,
        r.export_value_cad || '',
        r.profit || '',
        r.listing_url || '',
        r.carfax_link || '',
        r.status
    ]);
    
    const csv = [headers.join(','), ...rows.map(r => r.map(v => `"${v}"`).join(','))].join('\n');
    downloadFile(csv, `signal_vin_results_${getTimestamp()}.csv`, 'text/csv');
}

function downloadFile(content, filename, type) {
    const blob = new Blob([content], { type });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
}

function getTimestamp() {
    const now = new Date();
    return now.toISOString().replace(/[:.]/g, '-').substring(0, 19);
}

// ============================================
// INITIALIZE
// ============================================
document.addEventListener('DOMContentLoaded', () => {
    // Set initial prefix display
    document.getElementById('prefixesDisplay').textContent = getValidPrefixes().join(', ');
});