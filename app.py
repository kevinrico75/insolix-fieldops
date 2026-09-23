from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import contextmanager
import sqlite3, os, shutil, csv, io, secrets, hashlib, hmac, zipfile, base64, json, urllib.parse, urllib.request, urllib.error
from datetime import date, datetime, timedelta

BASE = os.path.dirname(__file__)
DATA_DIR = os.environ.get('INSOLIX_DATA_DIR', BASE)
os.makedirs(DATA_DIR, exist_ok=True)
DB = os.path.join(DATA_DIR, 'fieldops.db')
UPLOADS = os.path.join(DATA_DIR, 'uploads')
os.makedirs(UPLOADS, exist_ok=True)

# On first cloud start, seed the persistent data directory from the bundled local database.
BUNDLED_DB = os.path.join(BASE, 'fieldops.db')
if DATA_DIR != BASE and not os.path.exists(DB) and os.path.exists(BUNDLED_DB):
    shutil.copy2(BUNDLED_DB, DB)
BUNDLED_UPLOADS = os.path.join(BASE, 'uploads')
if DATA_DIR != BASE and os.path.isdir(BUNDLED_UPLOADS) and not os.listdir(UPLOADS):
    for name in os.listdir(BUNDLED_UPLOADS):
        src=os.path.join(BUNDLED_UPLOADS,name)
        dst=os.path.join(UPLOADS,name)
        if os.path.isfile(src): shutil.copy2(src,dst)

app = FastAPI(title='INSOLIX')
app.mount('/static', StaticFiles(directory=os.path.join(BASE, 'static')), name='static')
templates = Jinja2Templates(directory=os.path.join(BASE, 'templates'))

@contextmanager
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def cols(c, table):
    return {r['name'] for r in c.execute(f'PRAGMA table_info({table})').fetchall()}

def ensure_col(c, table, name, definition):
    if name not in cols(c, table):
        c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')

def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS customers(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, company TEXT, phone TEXT, email TEXT,
          address TEXT, type TEXT DEFAULT 'Customer', notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS leads(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, company TEXT, phone TEXT, email TEXT,
          address TEXT, source TEXT, trade TEXT, status TEXT DEFAULT 'New', assigned_to TEXT, value REAL DEFAULT 0,
          next_followup TEXT, notes TEXT, customer_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        CREATE TABLE IF NOT EXISTS estimates(
          id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL, lead_id INTEGER, trade TEXT NOT NULL,
          title TEXT NOT NULL, status TEXT DEFAULT 'Draft', sqft REAL DEFAULT 0, depth REAL DEFAULT 0, r_value REAL DEFAULT 0,
          material_cost REAL DEFAULT 0, labor_cost REAL DEFAULT 0, overhead_cost REAL DEFAULT 0, sell_price REAL DEFAULT 0,
          notes TEXT, sales_rep TEXT, estimator TEXT, project_manager TEXT, discount REAL DEFAULT 0, tax_rate REAL DEFAULT 0,
          expires_on TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(customer_id) REFERENCES customers(id), FOREIGN KEY(lead_id) REFERENCES leads(id)
        );
        CREATE TABLE IF NOT EXISTS estimate_phases(
          id INTEGER PRIMARY KEY AUTOINCREMENT, estimate_id INTEGER NOT NULL, name TEXT NOT NULL, sort_order INTEGER DEFAULT 0,
          notes TEXT, FOREIGN KEY(estimate_id) REFERENCES estimates(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS estimate_items(
          id INTEGER PRIMARY KEY AUTOINCREMENT, estimate_id INTEGER NOT NULL, phase_id INTEGER, pricebook_id INTEGER,
          description TEXT NOT NULL, qty REAL DEFAULT 0, unit TEXT DEFAULT 'EA', depth REAL DEFAULT 0, coverage REAL DEFAULT 0,
          material_unit_cost REAL DEFAULT 0, labor_unit_cost REAL DEFAULT 0, other_unit_cost REAL DEFAULT 0,
          unit_price REAL DEFAULT 0, discount REAL DEFAULT 0, optional INTEGER DEFAULT 0, selected INTEGER DEFAULT 1,
          sort_order INTEGER DEFAULT 0, FOREIGN KEY(estimate_id) REFERENCES estimates(id) ON DELETE CASCADE,
          FOREIGN KEY(phase_id) REFERENCES estimate_phases(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS pricebook(
          id INTEGER PRIMARY KEY AUTOINCREMENT, trade TEXT NOT NULL, category TEXT, code TEXT, name TEXT NOT NULL,
          unit TEXT DEFAULT 'SF', material_cost REAL DEFAULT 0, labor_cost REAL DEFAULT 0, other_cost REAL DEFAULT 0,
          sell_price REAL DEFAULT 0, default_depth REAL DEFAULT 0, coverage REAL DEFAULT 0, active INTEGER DEFAULT 1,
          notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS jobs(
          id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL, estimate_id INTEGER, title TEXT NOT NULL,
          trade TEXT NOT NULL, status TEXT DEFAULT 'Scheduled', scheduled_date TEXT, start_time TEXT, end_time TEXT,
          crew TEXT, truck TEXT, address TEXT, work_order TEXT, sales_rep TEXT, project_manager TEXT,
          actual_material REAL DEFAULT 0, actual_labor REAL DEFAULT 0, actual_other REAL DEFAULT 0,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(customer_id) REFERENCES customers(id),
          FOREIGN KEY(estimate_id) REFERENCES estimates(id)
        );
        CREATE TABLE IF NOT EXISTS work_orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, title TEXT NOT NULL, status TEXT DEFAULT 'Open',
          scheduled_date TEXT, crew TEXT, truck TEXT, instructions TEXT, completion_notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS invoices(
          id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL, job_id INTEGER, amount REAL DEFAULT 0,
          status TEXT DEFAULT 'Open', due_date TEXT, notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(customer_id) REFERENCES customers(id), FOREIGN KEY(job_id) REFERENCES jobs(id)
        );
        CREATE TABLE IF NOT EXISTS tasks(
          id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, due_date TEXT, assigned_to TEXT, status TEXT DEFAULT 'Open',
          priority TEXT DEFAULT 'Normal', related_type TEXT, related_id INTEGER, notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS activities(
          id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, description TEXT, related_type TEXT, related_id INTEGER,
          created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS documents(
          id INTEGER PRIMARY KEY AUTOINCREMENT, related_type TEXT NOT NULL, related_id INTEGER NOT NULL, filename TEXT NOT NULL,
          stored_name TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, role TEXT DEFAULT 'Admin',
          password_hash TEXT NOT NULL, password_salt TEXT NOT NULL, active INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS sessions(
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS signatures(
          id INTEGER PRIMARY KEY AUTOINCREMENT, estimate_id INTEGER NOT NULL, signer_name TEXT NOT NULL, signer_email TEXT,
          accepted_total REAL DEFAULT 0, accepted_at TEXT DEFAULT CURRENT_TIMESTAMP, ip_address TEXT,
          FOREIGN KEY(estimate_id) REFERENCES estimates(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS payments(
          id INTEGER PRIMARY KEY AUTOINCREMENT, invoice_id INTEGER NOT NULL, amount REAL NOT NULL, payment_date TEXT,
          method TEXT, reference TEXT, notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS employees(
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, role TEXT, phone TEXT, email TEXT, active INTEGER DEFAULT 1,
          notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS assets(
          id INTEGER PRIMARY KEY AUTOINCREMENT, asset_type TEXT NOT NULL, name TEXT NOT NULL, identifier TEXT, status TEXT DEFAULT 'Active',
          notes TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS quickbooks_connection(
          id INTEGER PRIMARY KEY CHECK(id=1), client_id TEXT, client_secret TEXT, realm_id TEXT, access_token TEXT,
          refresh_token TEXT, token_expires_at TEXT, refresh_expires_at TEXT, environment TEXT DEFAULT 'production',
          company_name TEXT, service_item_id TEXT, service_item_name TEXT, last_customer_sync TEXT, last_payment_sync TEXT,
          oauth_state TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS qbo_mappings(
          entity_type TEXT NOT NULL, local_id INTEGER NOT NULL, qbo_id TEXT NOT NULL, qbo_sync_token TEXT,
          last_synced_at TEXT DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(entity_type, local_id), UNIQUE(entity_type, qbo_id)
        );
        CREATE TABLE IF NOT EXISTS company_settings(
          id INTEGER PRIMARY KEY CHECK(id=1), company_name TEXT DEFAULT 'INSOLIX Thermal Solutions', phone TEXT, email TEXT, address TEXT,
          website TEXT, proposal_terms TEXT, invoice_terms TEXT, updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        ''')
        # Safe migrations from V1
        for name, definition in [('lead_id','INTEGER'),('sales_rep','TEXT'),('estimator','TEXT'),('project_manager','TEXT'),('discount','REAL DEFAULT 0'),('tax_rate','REAL DEFAULT 0'),('expires_on','TEXT')]:
            ensure_col(c,'estimates',name,definition)
        for name, definition in [('start_time','TEXT'),('end_time','TEXT'),('truck','TEXT'),('sales_rep','TEXT'),('project_manager','TEXT'),('actual_material','REAL DEFAULT 0'),('actual_labor','REAL DEFAULT 0'),('actual_other','REAL DEFAULT 0')]:
            ensure_col(c,'jobs',name,definition)
        for name, definition in [('related_type','TEXT'),('related_id','INTEGER')]:
            ensure_col(c,'activities',name,definition)
        for name, definition in [('manufacturer','TEXT'),('r_value','TEXT'),('facing','TEXT'),('size','TEXT'),('product_family','TEXT')]:
            ensure_col(c,'pricebook',name,definition)

        # Backfill V1 records so they open correctly in the richer V2 screens
        for e in c.execute('SELECT id FROM estimates').fetchall():
            if not c.execute('SELECT id FROM estimate_phases WHERE estimate_id=? LIMIT 1',(e['id'],)).fetchone():
                c.execute('INSERT INTO estimate_phases(estimate_id,name,sort_order) VALUES (?,?,?)',(e['id'],'Phase 1',1))
        for j in c.execute('SELECT id,title,scheduled_date,crew,truck,work_order FROM jobs').fetchall():
            if not c.execute('SELECT id FROM work_orders WHERE job_id=? LIMIT 1',(j['id'],)).fetchone():
                c.execute('INSERT INTO work_orders(job_id,title,scheduled_date,crew,truck,instructions) VALUES (?,?,?,?,?,?)',(j['id'],j['title'],j['scheduled_date'],j['crew'],j['truck'],j['work_order'] or ''))

        if c.execute('SELECT COUNT(*) n FROM pricebook').fetchone()['n'] == 0:
            items = [
                ('Insulation','Spray Foam','SF-OC','Open Cell Spray Foam','BF',0.36,0.16,0.05,0.85,1,0,'Open-cell spray foam per board foot'),
                ('Insulation','Spray Foam','SF-CC','Closed Cell Spray Foam','BF',0.78,0.22,0.08,1.55,1,0,'Closed-cell spray foam per board foot'),
                ('Insulation','Blown','BL-R49','Blown Fiberglass R-49','SF',0.82,0.55,0.10,2.15,0,0,'Attic blown fiberglass'),
                ('Insulation','Batt','BAT-R21','R-21 Fiberglass Batt','SF',0.70,0.48,0.08,1.90,0,0,'Wall batt insulation'),
                ('Masonry','CMU','CMU-8','8 inch CMU Wall','SF',4.25,6.75,1.15,17.50,0,0,'CMU wall labor and material'),
                ('Natural Stone','Veneer','NST-V','Natural Stone Veneer','SF',8.50,12.00,1.50,31.00,0,0,'Natural stone veneer installation'),
                ('Stucco','Exterior','STU-3C','3-Coat Stucco System','SF',2.80,5.50,0.90,13.50,0,0,'Three-coat stucco system'),
            ]
            c.executemany('''INSERT INTO pricebook(trade,category,code,name,unit,material_cost,labor_cost,other_cost,sell_price,default_depth,coverage,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', items)
        # Expanded INSOLIX insulation pricebook. Codes are stable so upgrades add missing items without duplicating them.
        insulation_catalog = []
        def pb(code, category, name, unit='SF', manufacturer='', r_value='', facing='', size='', family='', notes=''):
            insulation_catalog.append((code, category, name, unit, manufacturer, r_value, facing, size, family, notes))

        # Contractor-standard fiberglass batt/roll matrix. Generic variants cover field estimating; manufacturer families are below.
        batt_r = ['R-11','R-13','R-15','R-19','R-20','R-21','R-22','R-23','R-25','R-30','R-38','R-49']
        batt_widths = ['15 in','16 in','19 in','23 in','24 in']
        for rv in batt_r:
            for face in ['Unfaced','Kraft Faced']:
                for width in batt_widths:
                    code=f"BAT-{rv.replace('-','')}-{face.split()[0].upper()}-{width.split()[0]}"
                    pb(code,'Fiberglass Batt',f'{rv} Fiberglass Batt - {face} - {width}','SF','Generic / Field',rv,face,width,'Fiberglass Batt','Contractor estimating variant; confirm stocked package length and manufacturer before ordering.')
        # User-requested foil-faced residential batt options.
        for rv in ['R-19','R-30']:
            for width in ['15 in','16 in','19 in','23 in','24 in']:
                pb(f"BAT-{rv.replace('-','')}-FOIL-{width.split()[0]}",'Fiberglass Batt',f'{rv} Fiberglass Batt - Foil Faced - {width}','SF','Generic / Field',rv,'Foil Faced',width,'Fiberglass Batt','Foil-faced estimating variant; verify local availability before ordering.')

        # Owens Corning PINK Next Gen / PROPINK cavity products (family entries + common verified framing widths).
        for rv in ['R-11','R-13','R-15','R-19','R-20','R-21','R-30','R-38','R-49']:
            for width in ['15 in','23 in']:
                pb(f"OC-PNG-{rv.replace('-','')}-{width.split()[0]}",'Fiberglass Batt',f'Owens Corning PINK Next Gen {rv} Batt - {width}','SF','Owens Corning',rv,'Varies',width,'PINK Next Gen Fiberglas','Residential/wood-frame family entry; choose facing/package based on supplier availability.')
        for rv in ['R-11','R-13','R-15','R-21']:
            for width in ['16 in','24 in']:
                pb(f"OC-PNG-MF-{rv.replace('-','')}-{width.split()[0]}",'Fiberglass Batt',f'Owens Corning PINK Next Gen {rv} Metal Frame Batt - {width}','SF','Owens Corning',rv,'Varies',width,'PINK Next Gen Fiberglas','Metal-frame cavity family entry.')

        # Johns Manville formaldehyde-free fiberglass families. JM publishes R-11 through R-49 and 15/23 wood, 16/24 steel widths.
        for face, tag in [('Unfaced','UF'),('Kraft Faced','KF')]:
            for rv in ['R-11','R-13','R-15','R-19','R-21','R-22','R-25','R-30','R-38','R-49']:
                for width in ['15 in','23 in','16 in','24 in']:
                    pb(f"JM-{tag}-{rv.replace('-','')}-{width.split()[0]}",'Fiberglass Batt',f'Johns Manville {rv} {face} Fiberglass - {width}','SF','Johns Manville',rv,face,width,'Formaldehyde-free Fiberglass','JM family/width estimating entry; exact package length and regional availability vary.')

        # Loose-fill / blown fiberglass, with OC PROPINK L77 published attic values represented as estimate products.
        oc_l77 = [('R-13',4.75),('R-19',6.75),('R-22',7.75),('R-26',9.00),('R-30',10.25),('R-38',12.75),('R-44',14.75),('R-49',16.25),('R-60',19.50)]
        for rv,depth in oc_l77:
            pb(f"OC-L77-{rv.replace('-','')}",'Blown Fiberglass',f'Owens Corning PROPINK L77 Loosefill - {rv}','SF','Owens Corning',rv,'Unfaced',f'{depth:g} in min installed','PROPINK L77','Attic/open-cavity estimating entry; coverage varies by R-value and machine setup.')
        for fam in ['AttiCat Expanding Blown-In','PROPINK L77 Loosefill']:
            pb('OC-'+('ATTICAT' if fam.startswith('AttiCat') else 'L77-BAG'),'Blown Fiberglass',f'Owens Corning {fam} - Bag','Bag','Owens Corning','','Unfaced','Bag',fam,'Material purchase item; enter current supplier bag cost.')
        for fam in ['Attic Protector','Climate Pro','Spider Plus']:
            pb('JM-'+fam.upper().replace(' ','-'),'Blown Fiberglass',f'Johns Manville {fam} - Bag','Bag','Johns Manville','','Unfaced','Bag',fam,'Loose-fill/blown fiberglass material family; enter supplier-specific bag cost/coverage.')

        # Mineral wool / fire & sound.
        for rv,size,thk in [('R-15','15.25 x 47 in','3.5 in'),('R-15','23 x 47 in','3.5 in'),('R-23','15.25 x 47 in','5.5 in'),('R-23','23 x 47 in','5.5 in'),('R-30','15.25 x 47 in','7.25 in'),('R-30','23 x 47 in','7.25 in')]:
            pb(f"JM-TC-{rv.replace('-','')}-{size.split()[0].replace('.','')}",'Mineral Wool',f'Johns Manville TempControl {rv} Mineral Wool - {size}','SF','Johns Manville',rv,'Unfaced',size,'TempControl',f'Nominal thickness {thk}.')
        pb('JM-SFB','Mineral Wool','Johns Manville Sound & Fire Block Mineral Wool','SF','Johns Manville','','Unfaced','','Sound & Fire Block','Interior sound/fire-control batt family.')
        for rv in ['R-15','R-23','R-30']:
            pb(f"OC-TF-FSGP-{rv.replace('-','')}",'Mineral Wool',f'Owens Corning Thermafiber Fire & Sound Guard Plus - {rv}','SF','Owens Corning',rv,'Unfaced','','Thermafiber Fire & Sound Guard Plus','Residential/light-commercial mineral wool family; verify stocked size.')

        # Rigid foam / continuous insulation (non-HVAC, non-pipe).
        for thick in ['0.5 in','1 in','1.5 in','2 in','2.5 in','3 in']:
            pb(f"OC-FOM-{thick.replace(' ','').replace('.','')}",'Rigid Foam Board',f'Owens Corning FOAMULAR NGX XPS - {thick} - 4 x 8 ft','Sheet','Owens Corning','','Unfaced',f'{thick}, 4 x 8 ft','FOAMULAR NGX','Rigid XPS board for building-envelope applications; verify specific compressive-strength series required.')
        for thick in ['0.5 in','1 in','1.5 in','2 in','2.5 in','3 in']:
            pb(f"JM-AP-{thick.replace(' ','').replace('.','')}",'Rigid Foam Board',f'Johns Manville AP Foil-Faced Polyiso - {thick} - 4 x 8 ft','Sheet','Johns Manville','','Foil Faced',f'{thick}, 4 x 8 ft','AP Foil-Faced Polyiso','Continuous insulation/sheathing estimate item; verify current panel size and local availability.')

        # One-component foams, fireblocking and sealants. Brand-neutral and common manufacturer families.
        foam_items = [
          ('CAN-GAP-12','Air Sealing','Expanding Gap & Crack Foam - 12 oz','Can','Generic / Field','One-component polyurethane foam for gaps/cracks.'),
          ('CAN-GAP-20','Air Sealing','Pro Gun Gap & Crack Foam - 20-24 oz','Can','Generic / Field','Gun-grade one-component foam.'),
          ('CAN-WD','Air Sealing','Window & Door Low-Expansion Foam','Can','Generic / Field','Low-expansion foam for window/door perimeter sealing.'),
          ('CAN-FIRE-RED','Fireblocking','Red Fireblock Foam - Can','Can','Generic / Field','Fireblocking foam; use only where the specific listed product/system is permitted.'),
          ('GUN-FOAM','Air Sealing','Professional Foam Applicator Gun','EA','Generic / Field','Reusable can-foam applicator gun.'),
          ('GUN-CLEAN','Air Sealing','Foam Gun Cleaner - Can','Can','Generic / Field','Applicator/uncured foam cleaner.'),
          ('SEAL-ACRYLIC','Air Sealing','Acrylic/Latex Air Sealing Caulk - Tube','Tube','Generic / Field','General air-sealing sealant.'),
          ('SEAL-FIRE','Fireblocking','Fire-Rated Sealant / Firestop Caulk - Tube','Tube','Generic / Field','Use listed firestop product appropriate to the assembly.'),
          ('OC-ENERGY-SEAL','Air Sealing','Owens Corning EnergyComplete Sealant - Package','EA','Owens Corning','Air-sealing system family item.'),
        ]
        for code,cat,name,unit,mfg,notes in foam_items: pb(code,cat,name,unit,mfg,'','','','Air Sealing',notes)

        # Attic ventilation / baffles and dams.
        baffles = [
          ('OC-RAFT-R-MATE','Attic Ventilation','Owens Corning raft-R-mate Attic Rafter Vent - 22.5 x 48 in','EA','Owens Corning','22.5 x 48 in','Extruded-polystyrene rafter vent with optional air stop/insulation block.'),
          ('BAFFLE-16','Attic Ventilation','Rafter Vent / Air Baffle - 16 in cavity','EA','Generic / Field','16 in','Vent chute for soffit-to-attic airflow.'),
          ('BAFFLE-24','Attic Ventilation','Rafter Vent / Air Baffle - 24 in cavity','EA','Generic / Field','24 in','Vent chute for soffit-to-attic airflow.'),
          ('BAFFLE-EXT','Attic Ventilation','Rafter Baffle Extension','EA','Generic / Field','','Extension for attic vent chute.'),
          ('EAVE-DAM','Attic Ventilation','Eave Insulation Dam / Blocking','LF','Generic / Field','','Keeps loose-fill clear of soffit/ventilation path.'),
          ('ATTIC-RULER','Attic Accessories','Attic Insulation Depth Ruler','EA','Generic / Field','','Depth marker for blown attic insulation.'),
          ('ATTIC-HATCH','Attic Accessories','Attic Hatch / Stair Insulation Cover','EA','Generic / Field','','Insulated cover for attic access opening.'),
          ('OC-PINKCAP','Attic Accessories','Owens Corning PinkCap Attic Stair Insulator','EA','Owens Corning','','Attic stair insulation accessory family.'),
        ]
        for code,cat,name,unit,mfg,size,notes in baffles: pb(code,cat,name,unit,mfg,'','',size,'Attic Accessories',notes)

        # Building-envelope membranes, supports and installation consumables.
        accessories = [
          ('POLY-4MIL','Vapor Retarder','4 mil Polyethylene Vapor Barrier','SF','Generic / Field'),
          ('POLY-6MIL','Vapor Retarder','6 mil Polyethylene Vapor Barrier','SF','Generic / Field'),
          ('POLY-10MIL','Vapor Retarder','10 mil Reinforced Poly / Crawlspace Vapor Barrier','SF','Generic / Field'),
          ('NET-BLOW','Blown Insulation Accessories','Blown-In Insulation Netting','SF','Generic / Field'),
          ('NET-SPIDER','Blown Insulation Accessories','Dense-Pack / Spray-in-Blanket Netting','SF','Generic / Field'),
          ('WIRE-SUPPORT','Batt Accessories','Insulation Support Wire / Tiger Teeth','EA','Generic / Field'),
          ('ROD-SUPPORT','Batt Accessories','Wire Insulation Support Rod','EA','Generic / Field'),
          ('STAPLES','Batt Accessories','Insulation Staple Box','Box','Generic / Field'),
          ('KNIFE-INS','Tools & Consumables','Insulation Knife','EA','Generic / Field'),
          ('TAPE-FOIL','Tapes & Flashing','Foil-Faced Insulation Tape','Roll','Generic / Field'),
          ('TAPE-SHEATH','Tapes & Flashing','Sheathing / Air Barrier Tape','Roll','Generic / Field'),
          ('OC-FLASHSEAL','Tapes & Flashing','Owens Corning FlashSealR Flashing Tape','Roll','Owens Corning'),
          ('SILL-GASKET','Air Sealing','Sill Plate Foam Gasket','Roll','Generic / Field'),
          ('OC-FOAMSEALR','Air Sealing','Owens Corning FoamSealR Sill Plate Gasket','Roll','Owens Corning'),
          ('ATTIC-DAM','Blown Insulation Accessories','Attic Insulation Retaining Dam','LF','Generic / Field'),
          ('LIGHT-COVER','Attic Accessories','Recessed Light Insulation / Air-Seal Cover','EA','Generic / Field'),
          ('WEATHERSTRIP','Air Sealing','Attic Hatch Weatherstripping','Roll','Generic / Field'),
          ('STAPLE-HAMMER','Tools & Consumables','Hammer Tacker / Stapler','EA','Generic / Field'),
          ('BAGS-CONTRACTOR','Tools & Consumables','Heavy-Duty Contractor Cleanup Bags','Box','Generic / Field'),
        ]
        for code,cat,name,unit,mfg in accessories: pb(code,cat,name,unit,mfg,'','','','Accessories','Building-envelope/insulation installation item.')

        # Spray foam system / rig consumables (building insulation only; no HVAC or pipe insulation).
        spray = [
          ('SF-OC-BF','Spray Foam','Open Cell Spray Foam - Installed Board Foot','BF','Generic / Field'),
          ('SF-CC-BF','Spray Foam','Closed Cell Spray Foam - Installed Board Foot','BF','Generic / Field'),
          ('SF-KIT-OC','Spray Foam','Open Cell Two-Component Foam Kit','Kit','Generic / Field'),
          ('SF-KIT-CC','Spray Foam','Closed Cell Two-Component Foam Kit','Kit','Generic / Field'),
          ('SF-GUN','Spray Foam','Spray Foam Gun / Replacement Gun','EA','Generic / Field'),
          ('SF-MIX-CHAMBER','Spray Foam','Spray Foam Mix Chamber','EA','Generic / Field'),
          ('SF-GUN-SCREEN','Spray Foam','Spray Gun Screen / Filter','EA','Generic / Field'),
          ('SF-LUBE','Spray Foam','Spray Gun Lubricant / Grease','EA','Generic / Field'),
          ('SF-RELEASE','Spray Foam','Release Agent / Gun Cleaner','Can','Generic / Field'),
          ('SF-MASKING','Spray Foam','Masking Plastic / Overspray Protection','Roll','Generic / Field'),
        ]
        for code,cat,name,unit,mfg in spray: pb(code,cat,name,unit,mfg,'','','','Spray Foam','Spray-foam building-insulation estimating/consumable item.')

        # Insert only catalog codes not already present. Pricing intentionally starts at zero for supplier-specific setup.
        existing_codes={r['code'] for r in c.execute("SELECT code FROM pricebook WHERE code IS NOT NULL AND code<>''").fetchall()}
        for code,category,name,unit,mfg,rv,facing,size,family,notes in insulation_catalog:
            if code in existing_codes: continue
            c.execute('''INSERT INTO pricebook(trade,category,code,name,unit,material_cost,labor_cost,other_cost,sell_price,default_depth,coverage,active,notes,manufacturer,r_value,facing,size,product_family)
                         VALUES (?,?,?,?,?,0,0,0,0,0,0,1,?,?,?,?,?,?)''',
                      ('Insulation',category,code,name,unit,notes,mfg,rv,facing,size,family))
            existing_codes.add(code)
        ensure_col(c,'payments','qbo_payment_id','TEXT')
        ensure_col(c,'payments','source','TEXT')
        if not c.execute('SELECT id FROM company_settings WHERE id=1').fetchone():
            c.execute("INSERT INTO company_settings(id,company_name,proposal_terms,invoice_terms) VALUES (1,?,?,?)",('INSOLIX Thermal Solutions','Proposal valid through expiration date. Changes outside this scope require written approval.','Payment due by the invoice due date.'))
        if not c.execute('SELECT id FROM quickbooks_connection WHERE id=1').fetchone():
            c.execute("INSERT INTO quickbooks_connection(id,environment) VALUES (1,'production')")
        if c.execute('SELECT COUNT(*) n FROM users').fetchone()['n'] == 0:
            salt=secrets.token_hex(16); pw=hashlib.pbkdf2_hmac('sha256',b'admin123',bytes.fromhex(salt),200000).hex()
            c.execute('INSERT INTO users(name,email,role,password_hash,password_salt) VALUES (?,?,?,?,?)',('Administrator','admin@local','Admin',pw,salt))

def hash_password(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 200000).hex()

def verify_password(password, stored, salt):
    return hmac.compare_digest(hash_password(password, salt), stored)

def user_for_request(request):
    token=request.cookies.get('fieldops_session')
    if not token: return None
    with db() as c:
        return c.execute('SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND u.active=1',(token,)).fetchone()

@app.middleware('http')
async def auth_guard(request: Request, call_next):
    public = request.url.path.startswith('/static') or request.url.path.startswith('/documents/') or request.url.path.startswith('/proposal/') or request.url.path in ['/login','/health','/quickbooks/callback']
    u=user_for_request(request)
    request.state.user=u
    if not public and not u:
        return RedirectResponse('/login',303)
    if u and u['role']!='Admin' and request.url.path.startswith(('/settings','/team','/backup','/export')):
        return RedirectResponse('/',303)
    if u and u['role']=='Field' and request.url.path.startswith(('/reports','/pricebook','/invoices','/estimates','/leads')):
        return RedirectResponse('/schedule',303)
    response=await call_next(request)
    return response

@app.on_event('startup')
def startup(): init_db()

def money(v):
    try: return f"${float(v or 0):,.2f}"
    except: return '$0.00'

def pct(v):
    try: return f"{float(v or 0):.1f}%"
    except: return '0.0%'

templates.env.filters['money'] = money
templates.env.filters['pct'] = pct

def company_settings():
    with db() as c: return c.execute('SELECT * FROM company_settings WHERE id=1').fetchone()

templates.env.globals['company_settings'] = company_settings

def activity(c, kind, desc, related_type=None, related_id=None):
    c.execute('INSERT INTO activities(kind,description,related_type,related_id) VALUES (?,?,?,?)',(kind,desc,related_type,related_id))

def estimate_totals(c, estimate_id):
    e = c.execute('SELECT * FROM estimates WHERE id=?',(estimate_id,)).fetchone()
    items = c.execute('SELECT * FROM estimate_items WHERE estimate_id=? AND selected=1',(estimate_id,)).fetchall()
    if items:
        subtotal = sum((r['qty'] or 0)*(r['unit_price'] or 0)*(1-(r['discount'] or 0)/100) for r in items)
        material = sum((r['qty'] or 0)*(r['material_unit_cost'] or 0) for r in items)
        labor = sum((r['qty'] or 0)*(r['labor_unit_cost'] or 0) for r in items)
        other = sum((r['qty'] or 0)*(r['other_unit_cost'] or 0) for r in items)
    else:
        subtotal = e['sell_price'] or 0
        material = e['material_cost'] or 0
        labor = e['labor_cost'] or 0
        other = e['overhead_cost'] or 0
    discount_amt = subtotal*(e['discount'] or 0)/100
    taxable = subtotal-discount_amt
    tax = taxable*(e['tax_rate'] or 0)/100
    total = taxable+tax
    cost = material+labor+other
    profit = taxable-cost
    margin = profit/taxable*100 if taxable else 0
    return {'subtotal':subtotal,'discount_amt':discount_amt,'tax':tax,'total':total,'material':material,'labor':labor,'other':other,'cost':cost,'profit':profit,'margin':margin}

def invoice_totals(c, invoice_id):
    inv=c.execute('SELECT * FROM invoices WHERE id=?',(invoice_id,)).fetchone()
    paid=c.execute('SELECT COALESCE(SUM(amount),0) n FROM payments WHERE invoice_id=?',(invoice_id,)).fetchone()['n'] if inv else 0
    amount=inv['amount'] if inv else 0
    return {'amount':amount,'paid':paid,'balance':max(amount-paid,0)}



def qb_connection():
    with db() as c:
        return c.execute('SELECT * FROM quickbooks_connection WHERE id=1').fetchone()

def qb_credentials(conn=None):
    conn = conn or qb_connection()
    return {
        'client_id': os.getenv('QB_CLIENT_ID') or (conn['client_id'] if conn else ''),
        'client_secret': os.getenv('QB_CLIENT_SECRET') or (conn['client_secret'] if conn else ''),
    }

def qb_redirect_uri(request: Request):
    env_uri = os.getenv('QB_REDIRECT_URI')
    if env_uri: return env_uri
    return str(request.base_url).rstrip('/') + '/quickbooks/callback'

def qb_base(conn):
    return 'https://sandbox-quickbooks.api.intuit.com' if (conn['environment'] or 'production') == 'sandbox' else 'https://quickbooks.api.intuit.com'

def qb_connected(conn=None):
    conn = conn or qb_connection()
    return bool(conn and conn['realm_id'] and conn['refresh_token'])

def _http_json(url, method='GET', headers=None, data=None):
    body = None
    h = dict(headers or {})
    if data is not None:
        body = json.dumps(data).encode()
        h.setdefault('Content-Type','application/json')
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors='replace')
        raise RuntimeError(f'QuickBooks API error {e.code}: {raw[:1200]}')

def qb_refresh_if_needed(conn=None):
    conn = conn or qb_connection()
    if not qb_connected(conn): raise RuntimeError('QuickBooks is not connected.')
    exp = conn['token_expires_at']
    if conn['access_token'] and exp:
        try:
            if datetime.fromisoformat(exp) > datetime.utcnow() + timedelta(minutes=3): return conn
        except: pass
    creds = qb_credentials(conn)
    if not creds['client_id'] or not creds['client_secret']: raise RuntimeError('QuickBooks Client ID/Secret are missing.')
    basic = base64.b64encode(f"{creds['client_id']}:{creds['client_secret']}".encode()).decode()
    form = urllib.parse.urlencode({'grant_type':'refresh_token','refresh_token':conn['refresh_token']}).encode()
    req = urllib.request.Request('https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer', data=form, headers={'Authorization':f'Basic {basic}','Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as r: tok=json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError('QuickBooks token refresh failed: '+e.read().decode(errors='replace')[:1000])
    now=datetime.utcnow()
    with db() as c:
        c.execute('''UPDATE quickbooks_connection SET access_token=?,refresh_token=?,token_expires_at=?,refresh_expires_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=1''',(
            tok.get('access_token'),tok.get('refresh_token',conn['refresh_token']),
            (now+timedelta(seconds=int(tok.get('expires_in',3600)))).isoformat(),
            (now+timedelta(seconds=int(tok.get('x_refresh_token_expires_in',8726400)))).isoformat()))
    return qb_connection()

def qb_api(path, method='GET', data=None):
    conn=qb_refresh_if_needed()
    url=qb_base(conn)+f"/v3/company/{conn['realm_id']}{path}"
    sep='&' if '?' in url else '?'
    url += sep+'minorversion=75'
    return _http_json(url, method, {'Authorization':f"Bearer {conn['access_token']}",'Accept':'application/json'}, data)

def qb_query(sql):
    return qb_api('/query?query='+urllib.parse.quote(sql, safe=''))

def qb_customer_payload(cust):
    display=(cust['company'] or cust['name'] or f"Customer {cust['id']}").strip()
    p={'DisplayName':display}
    if cust['name']:
        parts=cust['name'].strip().split(' ',1); p['GivenName']=parts[0]
        if len(parts)>1: p['FamilyName']=parts[1]
    if cust['company']: p['CompanyName']=cust['company']
    if cust['email']: p['PrimaryEmailAddr']={'Address':cust['email']}
    if cust['phone']: p['PrimaryPhone']={'FreeFormNumber':cust['phone']}
    if cust['address']: p['BillAddr']={'Line1':cust['address']}
    return p

def qb_map(c, entity_type, local_id, qbo_id, sync_token=None):
    c.execute('''INSERT INTO qbo_mappings(entity_type,local_id,qbo_id,qbo_sync_token,last_synced_at) VALUES (?,?,?,?,CURRENT_TIMESTAMP)
                 ON CONFLICT(entity_type,local_id) DO UPDATE SET qbo_id=excluded.qbo_id,qbo_sync_token=excluded.qbo_sync_token,last_synced_at=CURRENT_TIMESTAMP''',(entity_type,local_id,str(qbo_id),sync_token))

def qb_ensure_customer(local_customer_id):
    with db() as c:
        m=c.execute("SELECT * FROM qbo_mappings WHERE entity_type='customer' AND local_id=?",(local_customer_id,)).fetchone()
        if m: return m['qbo_id']
        cust=c.execute('SELECT * FROM customers WHERE id=?',(local_customer_id,)).fetchone()
    if not cust: raise RuntimeError('Customer not found.')
    if cust['email']:
        email=str(cust['email']).replace("'","\\'")
        res=qb_query(f"SELECT * FROM Customer WHERE PrimaryEmailAddr = '{email}' MAXRESULTS 10")
        rows=(res.get('QueryResponse') or {}).get('Customer') or []
        if rows:
            q=rows[0]
            with db() as c: qb_map(c,'customer',local_customer_id,q['Id'],q.get('SyncToken'))
            return q['Id']
    payload=qb_customer_payload(cust)
    res=qb_api('/customer','POST',payload); q=res.get('Customer') or {}
    if not q.get('Id'): raise RuntimeError('QuickBooks did not return a customer ID.')
    with db() as c: qb_map(c,'customer',local_customer_id,q['Id'],q.get('SyncToken'))
    return q['Id']

def qb_service_item(conn=None):
    conn=conn or qb_connection()
    if conn and conn['service_item_id']: return conn['service_item_id'], conn['service_item_name'] or 'Services'
    res=qb_query("SELECT * FROM Item WHERE Active = true MAXRESULTS 100")
    rows=(res.get('QueryResponse') or {}).get('Item') or []
    svc=next((x for x in rows if x.get('Type')=='Service'), rows[0] if rows else None)
    if not svc: raise RuntimeError('No active QuickBooks Product/Service item was found. Create a Service item in QuickBooks, then sync again.')
    with db() as c: c.execute('UPDATE quickbooks_connection SET service_item_id=?,service_item_name=? WHERE id=1',(svc['Id'],svc.get('Name','Services')))
    return svc['Id'],svc.get('Name','Services')

def qb_push_invoice(local_invoice_id, send=False):
    with db() as c:
        inv=c.execute('''SELECT i.*,c.email,c.name,c.company,j.title job_title FROM invoices i JOIN customers c ON c.id=i.customer_id LEFT JOIN jobs j ON j.id=i.job_id WHERE i.id=?''',(local_invoice_id,)).fetchone()
        m=c.execute("SELECT * FROM qbo_mappings WHERE entity_type='invoice' AND local_id=?",(local_invoice_id,)).fetchone()
    if not inv: raise RuntimeError('Invoice not found.')
    if m:
        qid=m['qbo_id']
    else:
        qcust=qb_ensure_customer(inv['customer_id']); item_id,item_name=qb_service_item()
        desc=inv['job_title'] or inv['notes'] or f'INSOLIX Invoice #{local_invoice_id}'
        payload={'CustomerRef':{'value':str(qcust)},'Line':[{'Amount':round(float(inv['amount'] or 0),2),'DetailType':'SalesItemLineDetail','Description':desc,'SalesItemLineDetail':{'ItemRef':{'value':str(item_id),'name':item_name},'Qty':1,'UnitPrice':round(float(inv['amount'] or 0),2)}}]}
        if inv['due_date']: payload['DueDate']=inv['due_date']
        if inv['email']: payload['BillEmail']={'Address':inv['email']}
        payload['AllowOnlineCreditCardPayment']=True; payload['AllowOnlineACHPayment']=True
        res=qb_api('/invoice','POST',payload); q=res.get('Invoice') or {}; qid=q.get('Id')
        if not qid: raise RuntimeError('QuickBooks did not return an invoice ID.')
        with db() as c:
            qb_map(c,'invoice',local_invoice_id,qid,q.get('SyncToken'))
            activity(c,'quickbooks',f'Pushed invoice #{local_invoice_id} to QuickBooks as #{q.get("DocNumber") or qid}','invoice',local_invoice_id)
    if send:
        email=inv['email']
        if not email: raise RuntimeError('Customer email is required before QuickBooks can send this invoice.')
        qb_api(f'/invoice/{qid}/send?sendTo='+urllib.parse.quote(email,safe='@._+-'),'POST')
        with db() as c:
            c.execute("UPDATE invoices SET status='Sent' WHERE id=? AND status='Open'",(local_invoice_id,))
            activity(c,'quickbooks',f'QuickBooks emailed invoice #{local_invoice_id} to {email}','invoice',local_invoice_id)
    return qid

@app.get('/', response_class=HTMLResponse)
def dashboard(request: Request):
    today = date.today().isoformat()
    month = date.today().strftime('%Y-%m')
    with db() as c:
        metrics = {
            'leads': c.execute("SELECT COUNT(*) n FROM leads WHERE status NOT IN ('Won','Lost')").fetchone()['n'],
            'pending_estimates': c.execute("SELECT COUNT(*) n FROM estimates WHERE status IN ('Draft','Pending','Sent')").fetchone()['n'],
            'scheduled_jobs': c.execute("SELECT COUNT(*) n FROM jobs WHERE status IN ('Scheduled','In Progress','Unscheduled')").fetchone()['n'],
            'open_invoices': c.execute("SELECT COALESCE(SUM(amount),0) n FROM invoices WHERE status NOT IN ('Paid','Void')").fetchone()['n'],
            'sales': c.execute("SELECT COALESCE(SUM(sell_price),0) n FROM estimates WHERE status='Accepted' AND substr(created_at,1,7)=?",(month,)).fetchone()['n'],
        }
        jobs = c.execute('''SELECT j.*, c.name customer_name FROM jobs j JOIN customers c ON c.id=j.customer_id WHERE j.status NOT IN ('Completed','Cancelled') ORDER BY CASE WHEN j.scheduled_date IS NULL OR j.scheduled_date='' THEN 1 ELSE 0 END,j.scheduled_date LIMIT 10''').fetchall()
        estimates = c.execute('''SELECT e.*, c.name customer_name FROM estimates e JOIN customers c ON c.id=e.customer_id ORDER BY e.id DESC LIMIT 7''').fetchall()
        tasks = c.execute("SELECT * FROM tasks WHERE status!='Done' ORDER BY CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END,due_date LIMIT 8").fetchall()
        acts = c.execute('SELECT * FROM activities ORDER BY id DESC LIMIT 8').fetchall()
    return templates.TemplateResponse('dashboard.html', {'request':request,'metrics':metrics,'jobs':jobs,'estimates':estimates,'tasks':tasks,'activities':acts,'today':today})

@app.get('/leads', response_class=HTMLResponse)
def leads(request: Request):
    with db() as c:
        rows=c.execute('SELECT * FROM leads ORDER BY id DESC').fetchall()
    return templates.TemplateResponse('leads.html',{'request':request,'leads':rows})

@app.post('/leads')
def add_lead(name:str=Form(...),company:str=Form(''),phone:str=Form(''),email:str=Form(''),address:str=Form(''),source:str=Form(''),trade:str=Form('Insulation'),assigned_to:str=Form(''),value:float=Form(0),next_followup:str=Form(''),notes:str=Form('')):
    with db() as c:
        c.execute('''INSERT INTO leads(name,company,phone,email,address,source,trade,assigned_to,value,next_followup,notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',(name,company,phone,email,address,source,trade,assigned_to,value,next_followup,notes))
        lid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']; activity(c,'lead',f'New lead: {name}','lead',lid)
    return RedirectResponse('/leads',303)

@app.post('/leads/{lead_id}/status')
def lead_status(lead_id:int,status:str=Form(...)):
    with db() as c:
        c.execute('UPDATE leads SET status=? WHERE id=?',(status,lead_id)); activity(c,'lead',f'Lead #{lead_id} changed to {status}','lead',lead_id)
    return RedirectResponse('/leads',303)

@app.post('/leads/{lead_id}/convert')
def lead_convert(lead_id:int):
    with db() as c:
        l=c.execute('SELECT * FROM leads WHERE id=?',(lead_id,)).fetchone()
        if not l: return RedirectResponse('/leads',303)
        cid=l['customer_id']
        if not cid:
            c.execute('INSERT INTO customers(name,company,phone,email,address,type,notes) VALUES (?,?,?,?,?,?,?)',(l['name'],l['company'],l['phone'],l['email'],l['address'],'Customer',l['notes']))
            cid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']
            c.execute('UPDATE leads SET customer_id=?,status=? WHERE id=?',(cid,'Qualified',lead_id))
        c.execute('''INSERT INTO estimates(customer_id,lead_id,trade,title,status,sell_price,notes) VALUES (?,?,?,?,?,?,?)''',(cid,lead_id,l['trade'] or 'Insulation',f"{l['trade'] or 'Project'} Estimate",'Draft',l['value'] or 0,l['notes'] or ''))
        eid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']
        c.execute('INSERT INTO estimate_phases(estimate_id,name,sort_order) VALUES (?,?,?)',(eid,'Phase 1',1))
        activity(c,'estimate',f'Created estimate #{eid} from lead {l["name"]}','estimate',eid)
    return RedirectResponse(f'/estimates/{eid}',303)

@app.get('/customers', response_class=HTMLResponse)
def customers(request: Request):
    with db() as c: rows=c.execute('SELECT * FROM customers ORDER BY id DESC').fetchall()
    return templates.TemplateResponse('customers.html', {'request':request,'customers':rows})

@app.post('/customers')
def add_customer(name:str=Form(...),company:str=Form(''),phone:str=Form(''),email:str=Form(''),address:str=Form(''),type:str=Form('Customer'),notes:str=Form('')):
    with db() as c:
        c.execute('INSERT INTO customers(name,company,phone,email,address,type,notes) VALUES (?,?,?,?,?,?,?)',(name,company,phone,email,address,type,notes))
        cid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']; activity(c,'customer',f'Added customer {name}','customer',cid)
    return RedirectResponse(f'/customers/{cid}',303)

@app.get('/customers/{customer_id}', response_class=HTMLResponse)
def customer_detail(request:Request, customer_id:int):
    with db() as c:
        cust=c.execute('SELECT * FROM customers WHERE id=?',(customer_id,)).fetchone()
        est=c.execute('SELECT * FROM estimates WHERE customer_id=? ORDER BY id DESC',(customer_id,)).fetchall()
        jobs=c.execute('SELECT * FROM jobs WHERE customer_id=? ORDER BY id DESC',(customer_id,)).fetchall()
        inv=c.execute('SELECT * FROM invoices WHERE customer_id=? ORDER BY id DESC',(customer_id,)).fetchall()
        qbo=c.execute("SELECT * FROM qbo_mappings WHERE entity_type='customer' AND local_id=?",(customer_id,)).fetchone()
    return templates.TemplateResponse('customer_detail.html',{'request':request,'customer':cust,'estimates':est,'jobs':jobs,'invoices':inv,'qbo':qbo,'qb_connected':qb_connected()})

@app.get('/pricebook', response_class=HTMLResponse)
def pricebook(request:Request):
    with db() as c: rows=c.execute('SELECT * FROM pricebook WHERE active=1 ORDER BY trade,category,name').fetchall()
    return templates.TemplateResponse('pricebook.html',{'request':request,'items':rows})

@app.post('/pricebook')
def add_pricebook(trade:str=Form(...),category:str=Form(''),code:str=Form(''),name:str=Form(...),unit:str=Form('SF'),material_cost:float=Form(0),labor_cost:float=Form(0),other_cost:float=Form(0),sell_price:float=Form(0),default_depth:float=Form(0),coverage:float=Form(0),manufacturer:str=Form(''),r_value:str=Form(''),facing:str=Form(''),size:str=Form(''),product_family:str=Form(''),notes:str=Form('')):
    with db() as c:
        c.execute('''INSERT INTO pricebook(trade,category,code,name,unit,material_cost,labor_cost,other_cost,sell_price,default_depth,coverage,notes,manufacturer,r_value,facing,size,product_family) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(trade,category,code,name,unit,material_cost,labor_cost,other_cost,sell_price,default_depth,coverage,notes,manufacturer,r_value,facing,size,product_family))
        pid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']; activity(c,'pricebook',f'Added {name}','pricebook',pid)
    return RedirectResponse('/pricebook',303)

@app.get('/estimates', response_class=HTMLResponse)
def estimates(request: Request):
    with db() as c:
        rows=c.execute('''SELECT e.*,c.name customer_name,c.company FROM estimates e JOIN customers c ON c.id=e.customer_id ORDER BY e.id DESC''').fetchall()
        customers=c.execute('SELECT * FROM customers ORDER BY name').fetchall()
    return templates.TemplateResponse('estimates.html', {'request':request,'estimates':rows,'customers':customers})

@app.post('/estimates')
def add_estimate(customer_id:int=Form(...),trade:str=Form(...),title:str=Form(...),sales_rep:str=Form(''),estimator:str=Form(''),project_manager:str=Form(''),notes:str=Form('')):
    with db() as c:
        c.execute('''INSERT INTO estimates(customer_id,trade,title,sales_rep,estimator,project_manager,notes,expires_on) VALUES (?,?,?,?,?,?,?,?)''',(customer_id,trade,title,sales_rep,estimator,project_manager,notes,(date.today()+timedelta(days=30)).isoformat()))
        eid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']
        c.execute('INSERT INTO estimate_phases(estimate_id,name,sort_order) VALUES (?,?,?)',(eid,'Phase 1',1)); activity(c,'estimate',f'Created estimate: {title}','estimate',eid)
    return RedirectResponse(f'/estimates/{eid}',303)

@app.get('/estimates/{estimate_id}', response_class=HTMLResponse)
def estimate_detail(request:Request, estimate_id:int):
    with db() as c:
        e=c.execute('''SELECT e.*,c.name customer_name,c.company,c.email,c.phone,c.address FROM estimates e JOIN customers c ON c.id=e.customer_id WHERE e.id=?''',(estimate_id,)).fetchone()
        phases=c.execute('SELECT * FROM estimate_phases WHERE estimate_id=? ORDER BY sort_order,id',(estimate_id,)).fetchall()
        items=c.execute('SELECT * FROM estimate_items WHERE estimate_id=? ORDER BY phase_id,sort_order,id',(estimate_id,)).fetchall()
        pb=c.execute('SELECT * FROM pricebook WHERE active=1 ORDER BY trade,category,name').fetchall()
        totals=estimate_totals(c,estimate_id)
        signature=c.execute('SELECT * FROM signatures WHERE estimate_id=? ORDER BY id DESC LIMIT 1',(estimate_id,)).fetchone()
    items_by_phase={p['id']:[] for p in phases}
    unphased=[]
    for i in items:
        (items_by_phase.get(i['phase_id'],unphased)).append(i)
    return templates.TemplateResponse('estimate_detail.html',{'request':request,'e':e,'phases':phases,'items_by_phase':items_by_phase,'unphased':unphased,'pricebook':pb,'totals':totals,'signature':signature})

@app.post('/estimates/{estimate_id}/phase')
def add_phase(estimate_id:int,name:str=Form(...),notes:str=Form('')):
    with db() as c:
        n=c.execute('SELECT COALESCE(MAX(sort_order),0)+1 n FROM estimate_phases WHERE estimate_id=?',(estimate_id,)).fetchone()['n']
        c.execute('INSERT INTO estimate_phases(estimate_id,name,sort_order,notes) VALUES (?,?,?,?)',(estimate_id,name,n,notes)); activity(c,'estimate',f'Added phase {name} to estimate #{estimate_id}','estimate',estimate_id)
    return RedirectResponse(f'/estimates/{estimate_id}',303)

@app.post('/estimates/{estimate_id}/item')
def add_estimate_item(estimate_id:int,phase_id:int=Form(...),pricebook_id:int=Form(0),description:str=Form(''),qty:float=Form(0),unit:str=Form('SF'),depth:float=Form(0),material_unit_cost:float=Form(0),labor_unit_cost:float=Form(0),other_unit_cost:float=Form(0),unit_price:float=Form(0),discount:float=Form(0),optional:int=Form(0)):
    with db() as c:
        pb=None
        if pricebook_id:
            pb=c.execute('SELECT * FROM pricebook WHERE id=?',(pricebook_id,)).fetchone()
        if pb:
            description=description or pb['name']; unit=pb['unit']; material_unit_cost=pb['material_cost']; labor_unit_cost=pb['labor_cost']; other_unit_cost=pb['other_cost']; unit_price=pb['sell_price']; depth=depth or pb['default_depth']
        c.execute('''INSERT INTO estimate_items(estimate_id,phase_id,pricebook_id,description,qty,unit,depth,material_unit_cost,labor_unit_cost,other_unit_cost,unit_price,discount,optional,selected) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(estimate_id,phase_id,pricebook_id or None,description,qty,unit,depth,material_unit_cost,labor_unit_cost,other_unit_cost,unit_price,discount,optional,0 if optional else 1))
        activity(c,'estimate',f'Added line item to estimate #{estimate_id}','estimate',estimate_id)
    return RedirectResponse(f'/estimates/{estimate_id}',303)

@app.post('/estimates/{estimate_id}/item/{item_id}/toggle')
def toggle_item(estimate_id:int,item_id:int):
    with db() as c:
        r=c.execute('SELECT selected FROM estimate_items WHERE id=? AND estimate_id=?',(item_id,estimate_id)).fetchone()
        if r: c.execute('UPDATE estimate_items SET selected=? WHERE id=?',(0 if r['selected'] else 1,item_id))
    return RedirectResponse(f'/estimates/{estimate_id}',303)

@app.post('/estimates/{estimate_id}/item/{item_id}/delete')
def delete_item(estimate_id:int,item_id:int):
    with db() as c: c.execute('DELETE FROM estimate_items WHERE id=? AND estimate_id=?',(item_id,estimate_id)); activity(c,'estimate',f'Removed line item from estimate #{estimate_id}','estimate',estimate_id)
    return RedirectResponse(f'/estimates/{estimate_id}',303)

@app.post('/estimates/{estimate_id}/settings')
def estimate_settings(estimate_id:int,discount:float=Form(0),tax_rate:float=Form(0),sales_rep:str=Form(''),estimator:str=Form(''),project_manager:str=Form(''),expires_on:str=Form('')):
    with db() as c: c.execute('UPDATE estimates SET discount=?,tax_rate=?,sales_rep=?,estimator=?,project_manager=?,expires_on=? WHERE id=?',(discount,tax_rate,sales_rep,estimator,project_manager,expires_on,estimate_id))
    return RedirectResponse(f'/estimates/{estimate_id}',303)

@app.post('/estimates/{estimate_id}/status')
def estimate_status(estimate_id:int,status:str=Form(...),next_url:str=Form('/estimates')):
    with db() as c:
        e=c.execute('SELECT * FROM estimates WHERE id=?',(estimate_id,)).fetchone(); totals=estimate_totals(c,estimate_id)
        c.execute('UPDATE estimates SET status=?,sell_price=?,material_cost=?,labor_cost=?,overhead_cost=? WHERE id=?',(status,totals['total'],totals['material'],totals['labor'],totals['other'],estimate_id))
        activity(c,'estimate',f'Estimate #{estimate_id} changed to {status}','estimate',estimate_id)
        if status=='Accepted':
            existing=c.execute('SELECT id FROM jobs WHERE estimate_id=?',(estimate_id,)).fetchone()
            if not existing:
                cust=c.execute('SELECT * FROM customers WHERE id=?',(e['customer_id'],)).fetchone()
                c.execute('''INSERT INTO jobs(customer_id,estimate_id,title,trade,status,address,work_order,sales_rep,project_manager) VALUES (?,?,?,?,?,?,?,?,?)''',(e['customer_id'],estimate_id,e['title'],e['trade'],'Unscheduled',cust['address'],e['notes'] or '',e['sales_rep'],e['project_manager']))
                jid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']
                c.execute('INSERT INTO work_orders(job_id,title,status,instructions) VALUES (?,?,?,?)',(jid,e['title'],'Open',e['notes'] or ''))
                if e['lead_id']: c.execute("UPDATE leads SET status='Won' WHERE id=?",(e['lead_id'],))
                activity(c,'job',f'Created job #{jid} from accepted estimate #{estimate_id}','job',jid)
    return RedirectResponse(next_url,303)

@app.get('/jobs', response_class=HTMLResponse)
def jobs(request: Request):
    with db() as c:
        rows=c.execute('''SELECT j.*,c.name customer_name,c.company FROM jobs j JOIN customers c ON c.id=j.customer_id ORDER BY CASE WHEN j.scheduled_date IS NULL OR j.scheduled_date='' THEN 1 ELSE 0 END,j.scheduled_date,j.id DESC''').fetchall()
        customers=c.execute('SELECT * FROM customers ORDER BY name').fetchall()
    return templates.TemplateResponse('jobs.html', {'request':request,'jobs':rows,'customers':customers})

@app.post('/jobs')
def add_job(customer_id:int=Form(...),title:str=Form(...),trade:str=Form(...),scheduled_date:str=Form(''),start_time:str=Form(''),end_time:str=Form(''),crew:str=Form(''),truck:str=Form(''),address:str=Form(''),work_order:str=Form('')):
    with db() as c:
        c.execute('''INSERT INTO jobs(customer_id,title,trade,scheduled_date,start_time,end_time,crew,truck,address,work_order,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',(customer_id,title,trade,scheduled_date,start_time,end_time,crew,truck,address,work_order,'Scheduled' if scheduled_date else 'Unscheduled'))
        jid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']; c.execute('INSERT INTO work_orders(job_id,title,scheduled_date,crew,truck,instructions) VALUES (?,?,?,?,?,?)',(jid,title,scheduled_date,crew,truck,work_order)); activity(c,'job',f'Created job: {title}','job',jid)
    return RedirectResponse(f'/jobs/{jid}',303)

@app.get('/jobs/{job_id}', response_class=HTMLResponse)
def job_detail(request:Request, job_id:int):
    with db() as c:
        j=c.execute('''SELECT j.*,c.name customer_name,c.company,c.phone,c.email FROM jobs j JOIN customers c ON c.id=j.customer_id WHERE j.id=?''',(job_id,)).fetchone()
        wo=c.execute('SELECT * FROM work_orders WHERE job_id=? ORDER BY id',(job_id,)).fetchall()
        inv=c.execute('SELECT * FROM invoices WHERE job_id=? ORDER BY id DESC',(job_id,)).fetchall()
        docs=c.execute("SELECT * FROM documents WHERE related_type='job' AND related_id=? ORDER BY id DESC",(job_id,)).fetchall()
        est_total=None
        if j and j['estimate_id']: est_total=estimate_totals(c,j['estimate_id'])
    return templates.TemplateResponse('job_detail.html',{'request':request,'j':j,'work_orders':wo,'invoices':inv,'documents':docs,'estimate_total':est_total})

@app.post('/jobs/{job_id}/schedule')
def job_schedule(job_id:int,scheduled_date:str=Form(''),start_time:str=Form(''),end_time:str=Form(''),crew:str=Form(''),truck:str=Form('')):
    with db() as c:
        c.execute("UPDATE jobs SET scheduled_date=?,start_time=?,end_time=?,crew=?,truck=?,status=CASE WHEN status='Unscheduled' THEN 'Scheduled' ELSE status END WHERE id=?",(scheduled_date,start_time,end_time,crew,truck,job_id))
        c.execute('UPDATE work_orders SET scheduled_date=?,crew=?,truck=? WHERE job_id=?',(scheduled_date,crew,truck,job_id)); activity(c,'schedule',f'Scheduled job #{job_id} for {scheduled_date}','job',job_id)
    return RedirectResponse(f'/jobs/{job_id}',303)

@app.post('/jobs/{job_id}/actuals')
def job_actuals(job_id:int,actual_material:float=Form(0),actual_labor:float=Form(0),actual_other:float=Form(0)):
    with db() as c: c.execute('UPDATE jobs SET actual_material=?,actual_labor=?,actual_other=? WHERE id=?',(actual_material,actual_labor,actual_other,job_id)); activity(c,'job',f'Updated actual job costs for #{job_id}','job',job_id)
    return RedirectResponse(f'/jobs/{job_id}',303)

@app.post('/jobs/{job_id}/status')
def job_status(job_id:int,status:str=Form(...),next_url:str=Form('/jobs')):
    with db() as c:
        c.execute('UPDATE jobs SET status=? WHERE id=?',(status,job_id)); activity(c,'job',f'Job #{job_id} changed to {status}','job',job_id)
        if status=='Completed': c.execute("UPDATE work_orders SET status='Completed' WHERE job_id=?",(job_id,))
    return RedirectResponse(next_url,303)

@app.get('/schedule', response_class=HTMLResponse)
def schedule(request:Request):
    with db() as c:
        rows=c.execute('''SELECT j.*,c.name customer_name FROM jobs j JOIN customers c ON c.id=j.customer_id WHERE j.status NOT IN ('Cancelled') ORDER BY CASE WHEN j.scheduled_date IS NULL OR j.scheduled_date='' THEN 1 ELSE 0 END,j.scheduled_date,j.start_time''').fetchall()
    return templates.TemplateResponse('schedule.html',{'request':request,'jobs':rows})

@app.get('/invoices', response_class=HTMLResponse)
def invoices(request: Request):
    with db() as c:
        rows=c.execute('''SELECT i.*,c.name customer_name,j.title job_title FROM invoices i JOIN customers c ON c.id=i.customer_id LEFT JOIN jobs j ON j.id=i.job_id ORDER BY i.id DESC''').fetchall()
        jobs=c.execute('''SELECT j.*,c.name customer_name FROM jobs j JOIN customers c ON c.id=j.customer_id ORDER BY j.id DESC''').fetchall()
        invoice_summary={r['id']:invoice_totals(c,r['id']) for r in rows}
    return templates.TemplateResponse('invoices.html', {'request':request,'invoices':rows,'jobs':jobs,'invoice_summary':invoice_summary})

@app.post('/invoices')
def add_invoice(job_id:int=Form(...),amount:float=Form(...),due_date:str=Form(''),notes:str=Form('')):
    with db() as c:
        j=c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone(); c.execute('INSERT INTO invoices(customer_id,job_id,amount,due_date,notes) VALUES (?,?,?,?,?)',(j['customer_id'],job_id,amount,due_date,notes)); iid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']; activity(c,'invoice',f'Created invoice #{iid} for job #{job_id}','invoice',iid)
    return RedirectResponse('/invoices',303)

@app.post('/jobs/{job_id}/invoice')
def invoice_from_job(job_id:int):
    with db() as c:
        j=c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        existing=c.execute('SELECT id FROM invoices WHERE job_id=? AND status != ?',(job_id,'Void')).fetchone()
        if not existing:
            amount=0
            if j['estimate_id']: amount=estimate_totals(c,j['estimate_id'])['total']
            c.execute('INSERT INTO invoices(customer_id,job_id,amount,status,due_date,notes) VALUES (?,?,?,?,?,?)',(j['customer_id'],job_id,amount,'Open',(date.today()+timedelta(days=15)).isoformat(),'Created from job completion'))
            iid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']; activity(c,'invoice',f'Created invoice #{iid} from job #{job_id}','invoice',iid)
    return RedirectResponse(f'/jobs/{job_id}',303)

@app.post('/invoices/{invoice_id}/status')
def invoice_status(invoice_id:int,status:str=Form(...)):
    with db() as c: c.execute('UPDATE invoices SET status=? WHERE id=?',(status,invoice_id)); activity(c,'invoice',f'Invoice #{invoice_id} changed to {status}','invoice',invoice_id)
    return RedirectResponse('/invoices',303)

@app.get('/tasks', response_class=HTMLResponse)
def tasks(request:Request):
    with db() as c: rows=c.execute("SELECT * FROM tasks ORDER BY CASE WHEN status='Done' THEN 1 ELSE 0 END,CASE WHEN due_date IS NULL OR due_date='' THEN 1 ELSE 0 END,due_date,id DESC").fetchall()
    return templates.TemplateResponse('tasks.html',{'request':request,'tasks':rows})

@app.post('/tasks')
def add_task(title:str=Form(...),due_date:str=Form(''),assigned_to:str=Form(''),priority:str=Form('Normal'),related_type:str=Form(''),related_id:int=Form(0),notes:str=Form('')):
    with db() as c: c.execute('INSERT INTO tasks(title,due_date,assigned_to,priority,related_type,related_id,notes) VALUES (?,?,?,?,?,?,?)',(title,due_date,assigned_to,priority,related_type,related_id or None,notes)); tid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']; activity(c,'task',f'Created task: {title}','task',tid)
    return RedirectResponse('/tasks',303)

@app.post('/tasks/{task_id}/status')
def task_status(task_id:int,status:str=Form(...)):
    with db() as c: c.execute('UPDATE tasks SET status=? WHERE id=?',(status,task_id))
    return RedirectResponse('/tasks',303)

@app.post('/jobs/{job_id}/documents')
def upload_job_document(job_id:int,file:UploadFile=File(...)):
    safe=''.join(ch for ch in file.filename if ch.isalnum() or ch in '._- ')[:120]
    stored=f"job{job_id}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{safe}"
    path=os.path.join(UPLOADS,stored)
    with open(path,'wb') as out: shutil.copyfileobj(file.file,out)
    with db() as c: c.execute('INSERT INTO documents(related_type,related_id,filename,stored_name) VALUES (?,?,?,?)',('job',job_id,file.filename,stored)); activity(c,'document',f'Uploaded {file.filename} to job #{job_id}','job',job_id)
    return RedirectResponse(f'/jobs/{job_id}',303)

@app.get('/documents/{doc_id}')
def get_document(doc_id:int):
    with db() as c: d=c.execute('SELECT * FROM documents WHERE id=?',(doc_id,)).fetchone()
    if not d: return RedirectResponse('/',303)
    return FileResponse(os.path.join(UPLOADS,d['stored_name']),filename=d['filename'])

@app.get('/api/pricebook/{item_id}')
def pricebook_api(item_id:int):
    with db() as c: r=c.execute('SELECT * FROM pricebook WHERE id=?',(item_id,)).fetchone()
    return dict(r) if r else {}

@app.get('/api/estimate-calc')
def estimate_calc(material:float=0,labor:float=0,overhead:float=0,sell:float=0,sqft:float=0,depth:float=0):
    cost=material+labor+overhead; profit=sell-cost; margin=(profit/sell*100) if sell else 0
    return {'cost':round(cost,2),'profit':round(profit,2),'margin':round(margin,1),'board_feet':round(sqft*depth,1)}

@app.get('/health')
def health():
    return {'ok': True, 'app':'INSOLIX'}

@app.get('/login', response_class=HTMLResponse)
def login_page(request:Request, error:str=''):
    return templates.TemplateResponse('login.html',{'request':request,'error':error})

@app.post('/login')
def login(email:str=Form(...), password:str=Form(...)):
    with db() as c:
        u=c.execute('SELECT * FROM users WHERE lower(email)=lower(?) AND active=1',(email.strip(),)).fetchone()
        if not u or not verify_password(password,u['password_hash'],u['password_salt']):
            return RedirectResponse('/login?error=Invalid%20email%20or%20password',303)
        token=secrets.token_urlsafe(32)
        c.execute('INSERT INTO sessions(token,user_id) VALUES (?,?)',(token,u['id']))
    r=RedirectResponse('/',303)
    r.set_cookie('fieldops_session',token,httponly=True,samesite='lax',max_age=60*60*24*14)
    return r

@app.post('/logout')
def logout(request:Request):
    token=request.cookies.get('fieldops_session')
    with db() as c:
        if token:
            c.execute('DELETE FROM sessions WHERE token=?',(token,))
    r=RedirectResponse('/login',303)
    r.delete_cookie('fieldops_session')
    return r

@app.get('/search', response_class=HTMLResponse)
def global_search(request:Request, q:str=''):
    q=q.strip(); results=[]
    if q:
        like=f'%{q}%'
        with db() as c:
            for r in c.execute('SELECT id,name,company,address FROM customers WHERE name LIKE ? OR company LIKE ? OR phone LIKE ? OR email LIKE ? OR address LIKE ? LIMIT 20',(like,like,like,like,like)).fetchall():
                results.append({'type':'Customer','title':r['name'],'sub':r['company'] or r['address'] or '', 'url':f"/customers/{r['id']}"})
            for r in c.execute('SELECT id,title,status FROM estimates WHERE title LIKE ? OR CAST(id AS TEXT)=? LIMIT 20',(like,q)).fetchall():
                results.append({'type':'Estimate','title':f"#{r['id']} {r['title']}",'sub':r['status'],'url':f"/estimates/{r['id']}"})
            for r in c.execute('SELECT id,title,status,address FROM jobs WHERE title LIKE ? OR address LIKE ? OR CAST(id AS TEXT)=? LIMIT 20',(like,like,q)).fetchall():
                results.append({'type':'Job','title':f"#{r['id']} {r['title']}",'sub':r['address'] or r['status'],'url':f"/jobs/{r['id']}"})
            for r in c.execute('SELECT id,name,status,company FROM leads WHERE name LIKE ? OR company LIKE ? OR phone LIKE ? OR email LIKE ? LIMIT 20',(like,like,like,like)).fetchall():
                results.append({'type':'Lead','title':r['name'],'sub':r['company'] or r['status'],'url':'/leads'})
    return templates.TemplateResponse('search.html',{'request':request,'q':q,'results':results})

@app.post('/estimates/{estimate_id}/item/{item_id}/edit')
def edit_estimate_item(estimate_id:int,item_id:int,description:str=Form(...),qty:float=Form(0),unit:str=Form('EA'),depth:float=Form(0),material_unit_cost:float=Form(0),labor_unit_cost:float=Form(0),other_unit_cost:float=Form(0),unit_price:float=Form(0),discount:float=Form(0)):
    with db() as c:
        c.execute('UPDATE estimate_items SET description=?,qty=?,unit=?,depth=?,material_unit_cost=?,labor_unit_cost=?,other_unit_cost=?,unit_price=?,discount=? WHERE id=? AND estimate_id=?',(description,qty,unit,depth,material_unit_cost,labor_unit_cost,other_unit_cost,unit_price,discount,item_id,estimate_id))
        activity(c,'estimate',f'Edited line item on estimate #{estimate_id}','estimate',estimate_id)
    return RedirectResponse(f'/estimates/{estimate_id}',303)

@app.post('/estimates/{estimate_id}/duplicate')
def duplicate_estimate(estimate_id:int):
    with db() as c:
        e=c.execute('SELECT * FROM estimates WHERE id=?',(estimate_id,)).fetchone()
        if not e:
            return RedirectResponse('/estimates',303)
        c.execute('INSERT INTO estimates(customer_id,lead_id,trade,title,status,notes,sales_rep,estimator,project_manager,discount,tax_rate,expires_on) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(e['customer_id'],e['lead_id'],e['trade'],e['title']+' Copy','Draft',e['notes'],e['sales_rep'],e['estimator'],e['project_manager'],e['discount'],e['tax_rate'],(date.today()+timedelta(days=30)).isoformat()))
        new_id=c.execute('SELECT last_insert_rowid() id').fetchone()['id']
        phase_map={}
        phases=c.execute('SELECT * FROM estimate_phases WHERE estimate_id=? ORDER BY id',(estimate_id,)).fetchall()
        for p in phases:
            c.execute('INSERT INTO estimate_phases(estimate_id,name,sort_order,notes) VALUES (?,?,?,?)',(new_id,p['name'],p['sort_order'],p['notes']))
            phase_map[p['id']]=c.execute('SELECT last_insert_rowid() id').fetchone()['id']
        items=c.execute('SELECT * FROM estimate_items WHERE estimate_id=? ORDER BY id',(estimate_id,)).fetchall()
        for i in items:
            c.execute('INSERT INTO estimate_items(estimate_id,phase_id,pricebook_id,description,qty,unit,depth,coverage,material_unit_cost,labor_unit_cost,other_unit_cost,unit_price,discount,optional,selected,sort_order) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(new_id,phase_map.get(i['phase_id']),i['pricebook_id'],i['description'],i['qty'],i['unit'],i['depth'],i['coverage'],i['material_unit_cost'],i['labor_unit_cost'],i['other_unit_cost'],i['unit_price'],i['discount'],i['optional'],i['selected'],i['sort_order']))
        activity(c,'estimate',f'Duplicated estimate #{estimate_id} as #{new_id}','estimate',new_id)
    return RedirectResponse(f'/estimates/{new_id}',303)

@app.get('/proposal/{estimate_id}', response_class=HTMLResponse)
def proposal(request:Request, estimate_id:int):
    with db() as c:
        e=c.execute('SELECT e.*,c.name customer_name,c.company,c.email,c.phone,c.address FROM estimates e JOIN customers c ON c.id=e.customer_id WHERE e.id=?',(estimate_id,)).fetchone()
        phases=c.execute('SELECT * FROM estimate_phases WHERE estimate_id=? ORDER BY sort_order,id',(estimate_id,)).fetchall()
        items=c.execute('SELECT * FROM estimate_items WHERE estimate_id=? AND selected=1 ORDER BY phase_id,sort_order,id',(estimate_id,)).fetchall()
        totals=estimate_totals(c,estimate_id)
        sig=c.execute('SELECT * FROM signatures WHERE estimate_id=? ORDER BY id DESC LIMIT 1',(estimate_id,)).fetchone()
        company=c.execute('SELECT * FROM company_settings WHERE id=1').fetchone()
    by={p['id']:[] for p in phases}
    for i in items:
        by.setdefault(i['phase_id'],[]).append(i)
    return templates.TemplateResponse('proposal.html',{'request':request,'e':e,'phases':phases,'items_by_phase':by,'totals':totals,'signature':sig,'company':company})

@app.post('/proposal/{estimate_id}/accept')
def accept_proposal(request:Request, estimate_id:int, signer_name:str=Form(...), signer_email:str=Form('')):
    with db() as c:
        totals=estimate_totals(c,estimate_id)
        c.execute('INSERT INTO signatures(estimate_id,signer_name,signer_email,accepted_total,ip_address) VALUES (?,?,?,?,?)',(estimate_id,signer_name,signer_email,totals['total'],request.client.host if request.client else ''))
        c.execute('UPDATE estimates SET status=?,sell_price=?,material_cost=?,labor_cost=?,overhead_cost=? WHERE id=?',('Accepted',totals['total'],totals['material'],totals['labor'],totals['other'],estimate_id))
        e=c.execute('SELECT * FROM estimates WHERE id=?',(estimate_id,)).fetchone()
        existing=c.execute('SELECT id FROM jobs WHERE estimate_id=?',(estimate_id,)).fetchone()
        if not existing:
            cust=c.execute('SELECT * FROM customers WHERE id=?',(e['customer_id'],)).fetchone()
            c.execute('INSERT INTO jobs(customer_id,estimate_id,title,trade,status,address,work_order,sales_rep,project_manager) VALUES (?,?,?,?,?,?,?,?,?)',(e['customer_id'],estimate_id,e['title'],e['trade'],'Unscheduled',cust['address'],e['notes'] or '',e['sales_rep'],e['project_manager']))
            jid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']
            c.execute('INSERT INTO work_orders(job_id,title,status,instructions) VALUES (?,?,?,?)',(jid,e['title'],'Open',e['notes'] or ''))
        if e['lead_id']:
            c.execute("UPDATE leads SET status='Won' WHERE id=?",(e['lead_id'],))
        activity(c,'signature',f'{signer_name} accepted estimate #{estimate_id}','estimate',estimate_id)
    return RedirectResponse(f'/proposal/{estimate_id}',303)

@app.get('/invoices/{invoice_id}', response_class=HTMLResponse)
def invoice_detail(request:Request, invoice_id:int):
    with db() as c:
        inv=c.execute('SELECT i.*,c.name customer_name,c.company,c.email,c.phone,c.address,j.title job_title FROM invoices i JOIN customers c ON c.id=i.customer_id LEFT JOIN jobs j ON j.id=i.job_id WHERE i.id=?',(invoice_id,)).fetchone()
        payments=c.execute('SELECT * FROM payments WHERE invoice_id=? ORDER BY payment_date,id',(invoice_id,)).fetchall()
        totals=invoice_totals(c,invoice_id)
        company=c.execute('SELECT * FROM company_settings WHERE id=1').fetchone()
        qbo=c.execute("SELECT * FROM qbo_mappings WHERE entity_type='invoice' AND local_id=?",(invoice_id,)).fetchone()
    return templates.TemplateResponse('invoice_detail.html',{'request':request,'inv':inv,'payments':payments,'totals':totals,'company':company,'qbo':qbo,'qb_connected':qb_connected()})

@app.post('/invoices/{invoice_id}/payments')
def add_payment(invoice_id:int, amount:float=Form(...), payment_date:str=Form(''), method:str=Form('Other'), reference:str=Form(''), notes:str=Form('')):
    with db() as c:
        c.execute('INSERT INTO payments(invoice_id,amount,payment_date,method,reference,notes) VALUES (?,?,?,?,?,?)',(invoice_id,amount,payment_date or date.today().isoformat(),method,reference,notes))
        t=invoice_totals(c,invoice_id)
        status='Paid' if t['balance']<=0.005 else 'Partial'
        c.execute('UPDATE invoices SET status=? WHERE id=?',(status,invoice_id))
        activity(c,'payment',f'Recorded ${amount:,.2f} payment on invoice #{invoice_id}','invoice',invoice_id)
    return RedirectResponse(f'/invoices/{invoice_id}',303)

@app.get('/reports', response_class=HTMLResponse)
def reports(request:Request):
    with db() as c:
        accepted=c.execute("SELECT COUNT(*) n,COALESCE(SUM(sell_price),0) total FROM estimates WHERE status='Accepted'").fetchone()
        all_est=c.execute("SELECT COUNT(*) n FROM estimates WHERE status IN ('Sent','Pending','Accepted','Declined')").fetchone()['n']
        win_rate=(accepted['n']/all_est*100) if all_est else 0
        revenue=c.execute("SELECT COALESCE(SUM(amount),0) n FROM invoices WHERE status!='Void'").fetchone()['n']
        paid=c.execute('SELECT COALESCE(SUM(amount),0) n FROM payments').fetchone()['n']
        jobs=c.execute("SELECT COUNT(*) n FROM jobs WHERE status='Completed'").fetchone()['n']
        costs=c.execute("SELECT COALESCE(SUM(actual_material+actual_labor+actual_other),0) n FROM jobs WHERE status='Completed'").fetchone()['n']
        by_trade=c.execute("SELECT trade,COUNT(*) jobs,COALESCE(SUM(sell_price),0) sales FROM estimates WHERE status='Accepted' GROUP BY trade ORDER BY sales DESC").fetchall()
        by_source=c.execute("SELECT COALESCE(source,'Unknown') source,COUNT(*) leads,SUM(CASE WHEN status='Won' THEN 1 ELSE 0 END) won FROM leads GROUP BY COALESCE(source,'Unknown') ORDER BY leads DESC").fetchall()
    return templates.TemplateResponse('reports.html',{'request':request,'accepted':accepted,'win_rate':win_rate,'revenue':revenue,'paid':paid,'jobs_completed':jobs,'costs':costs,'by_trade':by_trade,'by_source':by_source})

@app.get('/team', response_class=HTMLResponse)
def team(request:Request):
    with db() as c:
        employees=c.execute('SELECT * FROM employees ORDER BY active DESC,name').fetchall()
        assets=c.execute('SELECT * FROM assets ORDER BY asset_type,name').fetchall()
        users=c.execute('SELECT id,name,email,role,active,created_at FROM users ORDER BY name').fetchall()
    return templates.TemplateResponse('team.html',{'request':request,'employees':employees,'assets':assets,'users':users})

@app.post('/team/employees')
def add_employee(name:str=Form(...),role:str=Form(''),phone:str=Form(''),email:str=Form(''),notes:str=Form('')):
    with db() as c:
        c.execute('INSERT INTO employees(name,role,phone,email,notes) VALUES (?,?,?,?,?)',(name,role,phone,email,notes))
    return RedirectResponse('/team',303)

@app.post('/team/assets')
def add_asset(asset_type:str=Form(...),name:str=Form(...),identifier:str=Form(''),status:str=Form('Active'),notes:str=Form('')):
    with db() as c:
        c.execute('INSERT INTO assets(asset_type,name,identifier,status,notes) VALUES (?,?,?,?,?)',(asset_type,name,identifier,status,notes))
    return RedirectResponse('/team',303)

@app.post('/team/users')
def add_user(name:str=Form(...),email:str=Form(...),role:str=Form('Office'),password:str=Form(...)):
    salt=secrets.token_hex(16); ph=hash_password(password,salt)
    with db() as c:
        try:
            c.execute('INSERT INTO users(name,email,role,password_hash,password_salt) VALUES (?,?,?,?,?)',(name,email,role,ph,salt))
        except sqlite3.IntegrityError:
            pass
    return RedirectResponse('/team',303)

@app.get('/settings', response_class=HTMLResponse)
def settings_page(request:Request):
    with db() as c:
        company=c.execute('SELECT * FROM company_settings WHERE id=1').fetchone()
        qb=c.execute('SELECT * FROM quickbooks_connection WHERE id=1').fetchone()
    return templates.TemplateResponse('settings.html',{'request':request,'company':company,'qb':qb,'qb_connected':qb_connected(qb),'qb_redirect_uri':qb_redirect_uri(request)})

@app.post('/settings')
def save_settings(company_name:str=Form(...),phone:str=Form(''),email:str=Form(''),address:str=Form(''),website:str=Form(''),proposal_terms:str=Form(''),invoice_terms:str=Form('')):
    with db() as c:
        c.execute('UPDATE company_settings SET company_name=?,phone=?,email=?,address=?,website=?,proposal_terms=?,invoice_terms=?,updated_at=CURRENT_TIMESTAMP WHERE id=1',(company_name,phone,email,address,website,proposal_terms,invoice_terms))
    return RedirectResponse('/settings',303)


@app.post('/quickbooks/settings')
def quickbooks_settings(client_id:str=Form(''),client_secret:str=Form(''),environment:str=Form('production'),service_item_id:str=Form(''),service_item_name:str=Form('')):
    with db() as c:
        existing=c.execute('SELECT * FROM quickbooks_connection WHERE id=1').fetchone()
        secret=client_secret or (existing['client_secret'] if existing else '')
        c.execute('''UPDATE quickbooks_connection SET client_id=?,client_secret=?,environment=?,service_item_id=?,service_item_name=?,updated_at=CURRENT_TIMESTAMP WHERE id=1''',(client_id,secret,environment,service_item_id,service_item_name))
    return RedirectResponse('/settings?qb_saved=1',303)

@app.get('/quickbooks/connect')
def quickbooks_connect(request:Request):
    conn=qb_connection(); creds=qb_credentials(conn)
    if not creds['client_id'] or not creds['client_secret']: return RedirectResponse('/settings?qb_error=missing_credentials',303)
    state=secrets.token_urlsafe(24)
    with db() as c: c.execute('UPDATE quickbooks_connection SET oauth_state=? WHERE id=1',(state,))
    params={'client_id':creds['client_id'],'response_type':'code','scope':'com.intuit.quickbooks.accounting','redirect_uri':qb_redirect_uri(request),'state':state}
    return RedirectResponse('https://appcenter.intuit.com/connect/oauth2?'+urllib.parse.urlencode(params),302)

@app.get('/quickbooks/callback')
def quickbooks_callback(request:Request, code:str='', realmId:str='', state:str='', error:str=''):
    conn=qb_connection()
    if error or not code or not realmId or not conn or state != (conn['oauth_state'] or ''): return RedirectResponse('/settings?qb_error=oauth',303)
    creds=qb_credentials(conn); basic=base64.b64encode(f"{creds['client_id']}:{creds['client_secret']}".encode()).decode()
    form=urllib.parse.urlencode({'grant_type':'authorization_code','code':code,'redirect_uri':qb_redirect_uri(request)}).encode()
    req=urllib.request.Request('https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer',data=form,headers={'Authorization':f'Basic {basic}','Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=30) as r: tok=json.loads(r.read().decode())
    except Exception:
        return RedirectResponse('/settings?qb_error=token',303)
    now=datetime.utcnow()
    with db() as c:
        c.execute('''UPDATE quickbooks_connection SET realm_id=?,access_token=?,refresh_token=?,token_expires_at=?,refresh_expires_at=?,oauth_state=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=1''',(realmId,tok.get('access_token'),tok.get('refresh_token'),(now+timedelta(seconds=int(tok.get('expires_in',3600)))).isoformat(),(now+timedelta(seconds=int(tok.get('x_refresh_token_expires_in',8726400)))).isoformat()))
    try:
        info=qb_api('/companyinfo/'+realmId)
        cname=((info.get('CompanyInfo') or {}).get('CompanyName'))
        if cname:
            with db() as c: c.execute('UPDATE quickbooks_connection SET company_name=? WHERE id=1',(cname,))
    except: pass
    return RedirectResponse('/settings?qb_connected=1',303)

@app.post('/quickbooks/disconnect')
def quickbooks_disconnect():
    with db() as c:
        c.execute('''UPDATE quickbooks_connection SET realm_id=NULL,access_token=NULL,refresh_token=NULL,token_expires_at=NULL,refresh_expires_at=NULL,company_name=NULL,oauth_state=NULL WHERE id=1''')
    return RedirectResponse('/settings?qb_disconnected=1',303)

@app.post('/quickbooks/sync-customers')
def quickbooks_sync_customers():
    try:
        if not qb_connected(): raise RuntimeError('not connected')
        res=qb_query('SELECT * FROM Customer MAXRESULTS 1000')
        remote=(res.get('QueryResponse') or {}).get('Customer') or []
        with db() as c:
            for q in remote:
                m=c.execute("SELECT local_id FROM qbo_mappings WHERE entity_type='customer' AND qbo_id=?",(str(q.get('Id')),)).fetchone()
                lid=m['local_id'] if m else None
                email=((q.get('PrimaryEmailAddr') or {}).get('Address') or '').strip()
                display=(q.get('DisplayName') or '').strip()
                if not lid and email:
                    r=c.execute('SELECT id FROM customers WHERE lower(email)=lower(?) LIMIT 1',(email,)).fetchone(); lid=r['id'] if r else None
                if not lid and display:
                    r=c.execute('SELECT id FROM customers WHERE name=? OR company=? LIMIT 1',(display,display)).fetchone(); lid=r['id'] if r else None
                phone=((q.get('PrimaryPhone') or {}).get('FreeFormNumber') or '')
                addr=(q.get('BillAddr') or {}).get('Line1') or ''
                company=q.get('CompanyName') or ''
                if not lid:
                    name=' '.join(x for x in [q.get('GivenName'),q.get('FamilyName')] if x).strip() or display
                    c.execute('INSERT INTO customers(name,company,phone,email,address,type,notes) VALUES (?,?,?,?,?,?,?)',(name,company,phone,email,addr,'Customer','Imported from QuickBooks'))
                    lid=c.execute('SELECT last_insert_rowid() id').fetchone()['id']
                else:
                    c.execute('''UPDATE customers SET phone=CASE WHEN phone IS NULL OR phone='' THEN ? ELSE phone END,email=CASE WHEN email IS NULL OR email='' THEN ? ELSE email END,address=CASE WHEN address IS NULL OR address='' THEN ? ELSE address END,company=CASE WHEN company IS NULL OR company='' THEN ? ELSE company END WHERE id=?''',(phone,email,addr,company,lid))
                qb_map(c,'customer',lid,q['Id'],q.get('SyncToken'))
            locals_=c.execute("SELECT * FROM customers WHERE id NOT IN (SELECT local_id FROM qbo_mappings WHERE entity_type='customer')").fetchall()
        for cust in locals_:
            try: qb_ensure_customer(cust['id'])
            except: pass
        with db() as c: c.execute('UPDATE quickbooks_connection SET last_customer_sync=CURRENT_TIMESTAMP WHERE id=1')
        return RedirectResponse('/settings?qb_customer_sync=1',303)
    except Exception as e:
        return RedirectResponse('/settings?qb_error='+urllib.parse.quote(str(e)[:180]),303)

@app.post('/customers/{customer_id}/quickbooks-sync')
def quickbooks_sync_one_customer(customer_id:int):
    try:
        qb_ensure_customer(customer_id)
        return RedirectResponse(f'/customers/{customer_id}?qb_synced=1',303)
    except Exception as e:
        return RedirectResponse(f'/customers/{customer_id}?qb_error='+urllib.parse.quote(str(e)[:180]),303)

@app.post('/invoices/{invoice_id}/quickbooks')
def invoice_to_quickbooks(invoice_id:int, action:str=Form('push')):
    try:
        qb_push_invoice(invoice_id, send=(action=='send'))
        return RedirectResponse(f'/invoices/{invoice_id}?qb_{action}=1',303)
    except Exception as e:
        return RedirectResponse(f'/invoices/{invoice_id}?qb_error='+urllib.parse.quote(str(e)[:180]),303)

@app.post('/quickbooks/sync-payments')
def quickbooks_sync_payments():
    try:
        res=qb_query('SELECT * FROM Payment MAXRESULTS 1000')
        remote=(res.get('QueryResponse') or {}).get('Payment') or []
        with db() as c:
            invmap={str(r['qbo_id']):r['local_id'] for r in c.execute("SELECT * FROM qbo_mappings WHERE entity_type='invoice'").fetchall()}
            for q in remote:
                qpid=str(q.get('Id') or '')
                if not qpid or c.execute('SELECT id FROM payments WHERE qbo_payment_id=?',(qpid,)).fetchone(): continue
                for line in q.get('Line') or []:
                    for lk in line.get('LinkedTxn') or []:
                        if lk.get('TxnType')!='Invoice': continue
                        local_inv=invmap.get(str(lk.get('TxnId')))
                        if not local_inv: continue
                        amt=float(line.get('Amount') or 0)
                        if amt<=0: continue
                        c.execute('INSERT INTO payments(invoice_id,amount,payment_date,method,reference,notes,qbo_payment_id,source) VALUES (?,?,?,?,?,?,?,?)',(local_inv,amt,q.get('TxnDate') or date.today().isoformat(),'QuickBooks',q.get('PaymentRefNum') or '', 'Synced from QuickBooks',qpid,'QuickBooks'))
                        t=invoice_totals(c,local_inv); c.execute('UPDATE invoices SET status=? WHERE id=?',('Paid' if t['balance']<=0.005 else 'Partial',local_inv))
            c.execute('UPDATE quickbooks_connection SET last_payment_sync=CURRENT_TIMESTAMP WHERE id=1')
        return RedirectResponse('/settings?qb_payment_sync=1',303)
    except Exception as e:
        return RedirectResponse('/settings?qb_error='+urllib.parse.quote(str(e)[:180]),303)

@app.get('/backup')
def backup_download():
    mem=io.BytesIO()
    with zipfile.ZipFile(mem,'w',zipfile.ZIP_DEFLATED) as z:
        z.write(DB,'fieldops.db')
        if os.path.isdir(UPLOADS):
            for root,dirs,files in os.walk(UPLOADS):
                for f in files:
                    path=os.path.join(root,f)
                    z.write(path,os.path.join('uploads',f))
    mem.seek(0)
    return Response(mem.getvalue(),media_type='application/zip',headers={'Content-Disposition':f'attachment; filename=fieldops-backup-{date.today().isoformat()}.zip'})

@app.get('/export/customers.csv')
def export_customers():
    out=io.StringIO(); w=csv.writer(out)
    w.writerow(['ID','Name','Company','Phone','Email','Address','Type','Notes'])
    with db() as c:
        for r in c.execute('SELECT id,name,company,phone,email,address,type,notes FROM customers ORDER BY id').fetchall():
            w.writerow(list(r))
    return Response(out.getvalue(),media_type='text/csv',headers={'Content-Disposition':'attachment; filename=customers.csv'})

@app.post('/work-orders/{work_order_id}/update')
def update_work_order(work_order_id:int,status:str=Form(...),completion_notes:str=Form('')):
    with db() as c:
        w=c.execute('SELECT * FROM work_orders WHERE id=?',(work_order_id,)).fetchone()
        if w:
            c.execute('UPDATE work_orders SET status=?,completion_notes=? WHERE id=?',(status,completion_notes,work_order_id))
            if status=='In Progress':
                c.execute("UPDATE jobs SET status='In Progress' WHERE id=? AND status!='Completed'",(w['job_id'],))
            activity(c,'work order',f'Work order #{work_order_id} changed to {status}','job',w['job_id'])
            return RedirectResponse(f"/jobs/{w['job_id']}",303)
    return RedirectResponse('/jobs',303)

@app.post('/account/password')
def change_password(request:Request,current_password:str=Form(...),new_password:str=Form(...)):
    u=user_for_request(request)
    if not u or not verify_password(current_password,u['password_hash'],u['password_salt']) or len(new_password)<8:
        return RedirectResponse('/settings?password_error=1',303)
    salt=secrets.token_hex(16); ph=hash_password(new_password,salt)
    with db() as c:
        c.execute('UPDATE users SET password_hash=?,password_salt=? WHERE id=?',(ph,salt,u['id']))
    return RedirectResponse('/settings?password_changed=1',303)
