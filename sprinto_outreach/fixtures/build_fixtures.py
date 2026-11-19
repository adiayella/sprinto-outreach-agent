"""Generates the simulation fixtures (fake companies on reserved .example domains). Run: python -m sprinto_outreach.fixtures.build_fixtures"""
import json
from pathlib import Path

HERE = Path(__file__).parent


def page(title, body, links=()):
    nav = "".join(f'<a href="{u}">{t}</a>' for u, t in [("/", "Home"), ("/product", "Product"), ("/login", "Log in")])
    ls = "".join(f'<p><a href="{u}">{t}</a></p>' for u, t in links)
    paras = "".join(f"<p>{line}</p>" for line in body)
    return f"<html><head><title>{title}</title></head><body><nav>{nav}</nav><main><h1>{title}</h1>{paras}{ls}</main><footer>© 2026</footer></body></html>"


SITES = {
    "ledgerloop.example": {
        "/": page("LedgerLoop — spend management for finance teams", [
            "LedgerLoop is a cloud spend management platform for mid-market finance teams.",
            "Our SaaS platform connects to your bank accounts, cards and ERP and handles sensitive financial data for every transaction.",
            "Teams use LedgerLoop to close the books faster and control card spend across subsidiaries.",
        ], [("/security", "Security"), ("/pricing", "Pricing"), ("/customers", "Customers"), ("/careers", "Careers"), ("/about", "About us"),
          ("https://jobs.ashbyhq.com/ledgerloop", "All open jobs")]),
        "/security": page("Security at LedgerLoop", [
            "We completed our SOC 2 Type I audit and our SOC 2 Type II audit window is currently in progress.",
            "We are working towards ISO 27001 certification to support customers in Europe.",
            "LedgerLoop processes personal data of EU employees and is GDPR compliant through our Data Processing Agreement.",
            "Data is encrypted at rest and in transit and hosted on AWS in the US and EU regions.",
        ]),
        "/pricing": page("Pricing", [
            "Starter, Growth and Enterprise plans are available.",
            "The Enterprise plan includes SAML SSO, audit logs, custom data residency and a dedicated security review.",
        ]),
        "/customers": page("Customers", [
            "Trusted by finance teams at Northwind Bank, Helix Health and Brightline Insurance.",
            "Customers include regulated banks and insurers across the US and UK.",
        ]),
        "/careers": page("Careers at LedgerLoop", [
            "We're hiring! Open roles:",
            "GRC Analyst (Security & Compliance) — Remote, US",
            "Senior Security Engineer — Bangalore",
            "Enterprise Account Executive — London",
        ]),
        "/about": page("About LedgerLoop", [
            "LedgerLoop raised a $40M Series B led by Granite Ventures in September 2026.",
            "We are a team of 140 people across New York, London and Bangalore.",
        ]),
    },
    "medibook.example": {
        "/": page("MediBook — patient scheduling for modern clinics", [
            "MediBook is a cloud scheduling platform that clinics use to book appointments and send patient reminders.",
            "Our SaaS product stores patient appointment details and contact information for each clinic.",
        ], [("/pricing", "Pricing"), ("/about", "About")]),
        "/pricing": page("Plans", [
            "Clinic and Network plans are available.",
            "The Enterprise plan for hospital networks adds SSO, role-based access and a signed BAA.",
        ]),
        "/about": page("About MediBook", [
            "MediBook was founded by doctors who were tired of no-show appointments.",
            "Today we serve over 300 clinics in the US and Germany.",
        ], [("/trust", "Trust center"), ("/careers", "Join the team")]),
        "/trust": page("MediBook Trust Center", [
            "MediBook is HIPAA compliant and signs a BAA with every US customer.",
            "We are preparing for our first SOC 2 Type II audit, planned for early 2027.",
            "For clinics in Germany we process patient data under GDPR with EU data residency.",
        ]),
        "/careers": page("Careers", [
            "Open roles: Compliance Manager (HIPAA, SOC 2) — Remote",
            "Open roles: Backend Engineer — Berlin",
        ]),
        "/sitemap.xml": "<urlset><url><loc>https://medibook.example/trust</loc></url>"
                        "<url><loc>https://medibook.example/careers</loc></url>"
                        "<url><loc>https://medibook.example/pricing</loc></url></urlset>",
        "__search__": [
            {"text": "MediBook announced a $12M Series A in March 2026 to expand into European clinics.",
             "source_title": "news.example", "source_uri": "https://news.example/medibook-series-a"},
            {"text": "A video about scheduling apps.", "source_title": "youtube.com",
             "source_uri": "https://youtube.com/watch?v=x"}
        ],
    },
    "crumbandco.example": {
        "/": page("Crumb & Co — artisan bakery", [
            "Crumb & Co is a family-run artisan bakery in Portland baking sourdough bread every morning.",
            "Visit our bakery cafe or order a celebration cake for pickup.",
        ], [("/about", "Our story")]),
        "/about": page("Our story", [
            "We have been baking bread and pastries in Portland since 1998.",
        ]),
    },
    "stratoscale-cloud.example": {
        "/": page("Stratoscale Cloud — enterprise cloud infrastructure", [
            "Stratoscale Cloud is an enterprise cloud infrastructure platform used by Fortune 500 companies.",
            "Our SaaS observability suite processes customer data for thousands of global enterprises.",
        ], [("/about", "Company"), ("/security", "Trust")]),
        "/about": page("Company", [
            "Stratoscale Cloud is publicly traded on the NYSE under the ticker STRX.",
            "We employ more than 6,000 people worldwide.",
        ]),
        "/security": page("Trust", [
            "Stratoscale Cloud holds SOC 2 Type II, ISO 27001, ISO 27701, FedRAMP Moderate and PCI DSS certifications.",
        ]),
    },
    "quietlabs.example": {
        "/": page("QuietLabs — feature flags for product teams", [
            "QuietLabs is a SaaS feature-flag platform that engineering teams use to ship safely.",
            "Our cloud service stores customer configuration and user targeting data for every release.",
        ], [("/security", "Security"), ("/customers", "Customers"), ("/careers", "Careers")]),
        "/security": page("Security", [
            "QuietLabs is currently undergoing a SOC 2 Type II audit with an independent auditor.",
            "Our release logs help engineering teams get SOC 2 ready faster.",
            "Enterprise customers can request our security questionnaire and penetration test summary.",
        ]),
        "/customers": page("Customers", [
            "Used by enterprise engineering teams at Contoso Retail and Fabrikam Payments.",
        ]),
        "/careers": page("Careers", [
            "Open roles: Security Engineer (Compliance automation) — Remote",
        ]),
    },
}

NEWS = {
    "news.example": {
        "/medibook-series-a": page("MediBook raises $12M Series A", [
            "MediBook raised a $12M Series A in March 2026 led by Northstar Health Ventures.",
            "The funding will take MediBook's clinic scheduling platform into Europe.",
            "Another startup, BookWell, also raised a seed round this month.",
        ], [("https://medibook.example/", "MediBook")]),
    }
}

ATS = {
    "ashby:ledgerloop": {"jobs": [
        {"title": "GRC Analyst", "department": "Security & Compliance", "location": "Remote, US",
         "publishedAt": "2026-11-02T10:00:00Z", "isListed": True},
        {"title": "Senior Backend Engineer", "department": "Engineering", "location": "Bangalore",
         "publishedAt": "2026-10-20T10:00:00Z", "isListed": True},
    ]},
}

HUNTER = {
    "ledgerloop.example": {"data": {"organization": "LedgerLoop", "emails": [
        {"value": "priya.raman@ledgerloop.example", "type": "personal", "confidence": 94, "first_name": "Priya",
         "last_name": "Raman", "position": "Head of Security", "seniority": "executive", "department": "it",
         "linkedin": None, "sources": [{"domain": "ledgerloop.example"}]},
        {"value": "arjun@ledgerloop.example", "type": "personal", "confidence": 91, "first_name": "Arjun",
         "last_name": "Mehta", "position": "CTO & Co-founder", "seniority": "executive", "department": "executive",
         "linkedin": None, "sources": [{"domain": "ledgerloop.example"}]},
        {"value": "maya.lopez@ledgerloop.example", "type": "personal", "confidence": 88, "first_name": "Maya",
         "last_name": "Lopez", "position": "Marketing Manager", "seniority": "senior", "department": "marketing",
         "linkedin": None, "sources": []},
    ]}, "meta": {"results": 3}},
    "medibook.example": {"data": {"organization": "MediBook", "emails": [
        {"value": "sara@medibook.example", "type": "personal", "confidence": 88, "first_name": "Sara",
         "last_name": "Okafor", "position": "CEO & Co-founder", "seniority": "executive", "department": "executive",
         "linkedin": None, "sources": [{"domain": "medibook.example"}]},
        {"value": "tom.reed@medibook.example", "type": "personal", "confidence": 79, "first_name": "Tom",
         "last_name": "Reed", "position": "Sales Director", "seniority": "senior", "department": "sales",
         "linkedin": None, "sources": []},
    ]}, "meta": {"results": 2}},
    "quietlabs.example": {"data": {"organization": "QuietLabs", "emails": [
        {"value": "jo@quietlabs.example", "type": "personal", "confidence": 90, "first_name": "Jo",
         "last_name": "Park", "position": "Head of Marketing", "seniority": "executive", "department": "marketing",
         "linkedin": None, "sources": []},
        {"value": "sales@quietlabs.example", "type": "generic", "confidence": 95, "first_name": None,
         "last_name": None, "position": None, "seniority": None, "department": "sales", "linkedin": None, "sources": []},
    ]}, "meta": {"results": 2}},
}

if __name__ == "__main__":
    (HERE / "sites").mkdir(exist_ok=True)
    for d, pages in SITES.items():
        (HERE / "sites" / f"{d}.json").write_text(json.dumps(pages, indent=1))
    (HERE / "hunter.json").write_text(json.dumps(HUNTER, indent=1))
    for d, pages in NEWS.items():
        (HERE / "sites" / f"{d}.json").write_text(json.dumps(pages, indent=1))
    (HERE / "ats.json").write_text(json.dumps(ATS, indent=1))
    print("fixtures written:", ", ".join(SITES))
