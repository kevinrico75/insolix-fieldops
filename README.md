# Rico FieldOps - Company Operations Release

Rico FieldOps is a local-first field-service CRM, estimating, scheduling, job-management and invoicing application built for an insulation/masonry operation.

## Start on Windows
1. Right-click the downloaded ZIP > Properties > Unblock > Apply.
2. Extract the entire ZIP.
3. Double-click `START_WINDOWS.bat`.
4. Keep the terminal window open while using FieldOps.
5. The app opens at http://127.0.0.1:8000

## First login
- Email: `admin@local`
- Password: `admin123`

Add permanent users under **Team & Assets**. Field-role users are restricted from admin/financial setup areas.

## Included
- Leads/opportunity tracking and follow-up dates
- Customer/contractor CRM and history
- Pricebook for insulation, masonry, stucco and stone
- Multi-phase estimating with costs, margin, options, tax and discounts
- Editable and duplicatable estimates
- Public customer proposal page with print/PDF and electronic acceptance record
- Accepted estimate to job/work-order conversion
- Scheduling, crews/trucks, work orders and field completion notes
- Job photos and document uploads
- Estimated vs actual job costing
- Invoices, partial/full payments and balances
- Tasks/reminders
- Team, user roles, trucks/equipment
- Reports for sales, win rate, invoicing, collections and lead sources
- Global search
- Full database/document backup and customer CSV export

## Important production notes
This release runs locally on the Windows computer. Remote multi-device access requires deployment to a secured server/cloud environment. QuickBooks, SMS/email delivery, online card/ACH processing, and other third-party integrations require the corresponding vendor accounts/API credentials and are not enabled by default.

Back up regularly from **Settings > Download Full Backup**.

## QuickBooks Online integration

This build includes a QuickBooks Online integration layer for:
- OAuth 2.0 connection to a QuickBooks Online company
- Two-way customer synchronization
- Per-customer QuickBooks mapping IDs
- Creating FieldOps invoices in QuickBooks
- Sending a linked invoice through QuickBooks email
- Requesting online ACH / credit-card payment options on QuickBooks invoices when QuickBooks Payments is enabled
- Pulling QuickBooks payment records back into FieldOps

### First-time setup
1. Create an Intuit Developer app for QuickBooks Online Accounting.
2. In Intuit, add the redirect URI shown in FieldOps > Settings > QuickBooks Online.
3. Copy the Intuit Client ID and Client Secret into FieldOps > Settings.
4. Choose Sandbox for testing or Production for your real company.
5. Save, then click Connect to QuickBooks and authorize your company.
6. Click Sync Customers.
7. On a FieldOps invoice, click Create in QuickBooks or Send Through QuickBooks.

Important: online ACH/card collection requires QuickBooks Payments to be enabled for the connected QuickBooks company. Use Sandbox first before connecting production accounting data.
