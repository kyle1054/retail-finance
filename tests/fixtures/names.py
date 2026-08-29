"""Invented people, merchants and SKUs for the synthetic database.

Every string in this file is made up. Nothing here is a real person, a real
shop, a real mall or a real card statement descriptor — that is the whole point
of the file existing separately from the generators: one place to check that the
public build ships no real-world identity.

Names are combined by index, never sampled randomly, so the same seed profile
always produces the same roster.
"""

# ── People ────────────────────────────────────────────────────────────────────
# Stored as "Surname, Firstname" because that is the shape the payroll paste
# parser (northwind/deductions/payroll_sync.py) expects.

SURNAMES = [
    'Abernathy', 'Bramwell', 'Calderon', 'Dunmore', 'Estcourt', 'Fenwick',
    'Galbraith', 'Harkness', 'Ingleby', 'Jessop', 'Kentridge', 'Lorimer',
    'Marchetti', 'Norbury', 'Oakhurst', 'Pemberton', 'Quennell', 'Rothwell',
    'Sandiford', 'Tetlow', 'Underwood', 'Verrall', 'Wexford', 'Yarborough',
    'Zenner', 'Ashgrove', 'Blakemore', 'Coppersmith', 'Danvers', 'Ellingham',
    'Fothergill', 'Grimsby', 'Havelock', 'Ilminster', 'Jerrold', 'Kettleby',
    'Langbourne', 'Marlowe', 'Netherby', 'Ovingdean', 'Parslow', 'Quorn',
    'Ravensworth', 'Stannard', 'Thackery', 'Ulverston', 'Vellacott',
    'Warburton', 'Yelverton', 'Zouche', 'Athelney', 'Barrowclough',
    'Chetwynd', 'Dunstable', 'Edgeworth', 'Farnaby', 'Gorsuch', 'Hollingsby',
    'Inkpen', 'Jopling', 'Kesteven', 'Lyddington', 'Merriweather',
    'Nettleship', 'Orlebar', 'Postlethwaite', 'Quimby', 'Redmayne',
    'Swaffham', 'Trelawny', 'Uppington', 'Vanbrugh', 'Whitcombe', 'Yoxall',
    'Zebedee', 'Aldercott', 'Bellwether', 'Crowmarsh', 'Drewsteign',
    'Elverton', 'Fitzhardinge', 'Gaddesby', 'Hurstbourne', 'Ivelchester',
    'Jardyce', 'Kimmeridge', 'Lostwithiel', 'Mundesley', 'Nunburnholme',
    'Ottershaw', 'Pucklechurch', 'Quainton', 'Rowdeford', 'Silchester',
    'Tolpuddle', 'Ubberley', 'Vowchurch', 'Wolvercote', 'Yaverland',
    'Zelah', 'Amberwell', 'Bythesea', 'Charnwood', 'Dowlish',
]

GIVEN_NAMES = [
    'Amara', 'Bethan', 'Cassian', 'Delphine', 'Ewan', 'Fenella', 'Gideon',
    'Halina', 'Isolde', 'Jarrah', 'Kirabo', 'Linnea', 'Marek', 'Nomsa',
    'Oleander', 'Perpetua', 'Quintin', 'Rosalind', 'Solomon', 'Tamsin',
    'Ulric', 'Verity', 'Wilhelmina', 'Xanthe', 'Yolande', 'Zephyr',
    'Anouk', 'Bramwell', 'Corliss', 'Dorabella', 'Emrys', 'Florica',
    'Gwendal', 'Hesper', 'Ilario', 'Jocasta', 'Kestrel', 'Lucasta',
    'Mirabel', 'Nikolai', 'Orsolya', 'Padraig', 'Quilla', 'Rowena',
    'Seraphin', 'Thaddeus', 'Ursula', 'Valentin', 'Winnifred', 'Xavier',
]

JOB_TITLES = [
    'Ambassador', 'Senior Ambassador', 'Store Manager', 'Assistant Manager',
    'Stock Controller', 'Visual Merchandiser',
]

HQ_JOB_TITLES = [
    'Buyer', 'Finance Assistant', 'Payroll Administrator', 'Warehouse Lead',
    'Dispatch Clerk', 'People Partner',
]


def person(index):
    """A stable "Surname, Firstname" for `index`.

    Surnames and given names advance at co-prime-ish rates so no two indexes in
    a realistic roster size collide, and the generators assert uniqueness.
    """
    surname = SURNAMES[index % len(SURNAMES)]
    given = GIVEN_NAMES[(index * 7 + index // len(SURNAMES)) % len(GIVEN_NAMES)]
    return '%s, %s' % (surname, given)


# ── Merchandise ───────────────────────────────────────────────────────────────

UNIFORM_ITEMS = [
    ('UNI-1001', 'Field Jacket'), ('UNI-1002', 'Oxford Shirt'),
    ('UNI-1003', 'Canvas Trousers'), ('UNI-1004', 'Knit Pullover'),
    ('UNI-1005', 'Trail Boots'), ('UNI-1006', 'Rain Shell'),
    ('UNI-1007', 'Chambray Shirt'), ('UNI-1008', 'Wool Overshirt'),
]

LAYBY_ITEMS = [
    ('SKU-2201', 'Weekender Duffel', 149000),
    ('SKU-2202', 'Leather Belt', 39500),
    ('SKU-2203', 'Down Gilet', 189000),
    ('SKU-2204', 'Merino Beanie', 24500),
    ('SKU-2205', 'Waxed Backpack', 219000),
    ('SKU-2206', 'Linen Shirt', 89000),
    ('SKU-2207', 'Suede Loafers', 165000),
    ('SKU-2208', 'Travel Wallet', 54000),
]

ALLOWANCE_ITEMS = [
    ('ALW-3301', 'Everyday Tote', 79500),
    ('ALW-3302', 'Poplin Dress', 129000),
    ('ALW-3303', 'Cotton Socks (3 pack)', 19500),
    ('ALW-3304', 'Twill Cap', 34500),
]

UNDERCHARGE_REASONS = [
    'Discount applied twice at the till',
    'Wrong size scanned, cheaper line rang up',
    'Promotion applied outside its dates',
    'Manual price override not authorised',
    'Bundle price used for a single item',
    'Staff discount given to a non-staff customer',
]

OVERCHARGE_REASONS = [
    'Customer charged full price on a marked-down item',
    'Duplicate line rang up on the same sale',
]

# ── Credit-card statement descriptors ────────────────────────────────────────
# (descriptor, category, account code, account name) — the codes match
# northwind/cards/accounts.py so AI-coded lines look plausible.
#
# The LEADING token of each descriptor is the "brand" the coding memory keys on
# (northwind/cards/ai.merchant_brand), and a remembered brand is applied to any
# other line sharing it. So every brand here is invented and deliberately unlike
# any descriptor the tests themselves use — a collision would silently code a
# test's line from memory instead of sending it to the model, and the test would
# fail somewhere far away from the cause.

MERCHANTS = [
    ('PETROLEA MAIN ROAD', 'spend', '6230', 'Vehicle Costs'),
    ('PARKMASTER GARAGE', 'spend', '6410', 'Local Travel - Transport'),
    ('METROCAB TRIP 4471', 'spend', '6410', 'Local Travel - Transport'),
    ('PAPERCOUNT SUPPLIES', 'spend', '6240', 'Printing & Stationery'),
    ('BEANFIELD REFRESHMENTS', 'spend', '6330', 'Staff Amenities'),
    ('PARCELWING COURIER', 'spend', '6160', 'Delivery - Domestic Courier'),
    ('CLOUDLEDGER SUBSCRIPTION', 'spend', '6290', 'Software Subscriptions'),
    ('STILLWATER INN', 'spend', '6390', 'Local Travel - Accommodation'),
    ('GRIDPOINT ELECTRIC', 'spend', '6030', 'Utilities'),
    ('TOOLCRATE HARDWARE', 'spend', '6340', 'Repairs & Maintenance'),
    ('PIXELWORKS STUDIO', 'spend', '6110', 'Media Production'),
    ('MERIDIAN LEGAL', 'spend', '6280', 'Legal Services'),
]

CARD_FEES = [
    ('MONTHLY SERVICE FEE', 'fee'),
    ('CARD REPLACEMENT FEE', 'fee'),
]

CARD_TRANSFERS = [
    ('SETTLEMENT TRANSFER IN', 'transfer'),
]

CARDS = [
    ('Operations Credit Card', 'Operations', 'operations.card@northwind-apparel.example'),
    ('Marketing Credit Card', 'Marketing', 'marketing.card@northwind-apparel.example'),
    ('Logistics Credit Card', 'Logistics', 'logistics.card@northwind-apparel.example'),
    ('Property Credit Card', 'Property', 'property.card@northwind-apparel.example'),
]

# ── Regional managers ────────────────────────────────────────────────────────

REGIONAL_MANAGERS = [
    ('north.rm@northwind-apparel.example', 'Fenwick, Delphine'),
    ('south.rm@northwind-apparel.example', 'Lorimer, Marek'),
]

# ── Cash-recon note text ─────────────────────────────────────────────────────

CASH_NOTES = [
    'Corner shop', 'Depot run', 'Team lunch', 'Window cleaner',
    'Courier collection', 'Till float top up', 'Bank drop',
    'Refill for the kitchen', 'Replacement bulbs', 'Petty cash count',
]
