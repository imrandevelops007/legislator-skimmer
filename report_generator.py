from weasyprint import HTML
from jinja2 import Environment, FileSystemLoader
import os

def split_pipe(text):
    return [x.strip() for x in text.split("|") if x.strip()]

def get_party_color(party):
    return "#c62828" if party.lower() == "republican" else "#1565c0"

def generate_report(row):
    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("report.html")

    html = template.render(
        name=row["Legislator"],
        chamber=row["Chamber"],
        district=row["District"],
        party_color=get_party_color(row["Party"]),
        image_url=row["Image_URL"],

        committee=row["Committee_Relevance_Summary"],
        time_in_office=split_pipe(row["Time_In_Office_Summary"]),
        bio=split_pipe(row["Generated_Biography"]),
        issues=split_pipe(row["Key_Issues"]),
        district=split_pipe(row["District_Development_Signals"]),
        focus=split_pipe(row["Legislative_Focus_Areas"]),
        bills=split_pipe(row["Key_Bills"]),
        positioning=row["Political_Positioning"],
        positioning_notes=split_pipe(row["Political_Positioning_Bullets"]),
        sbdc=row["SBDC_Framing"],
        talking=split_pipe(row["Talking_Points"]),
    )

    output_path = f"reports/{row['Legislator']}.pdf"
    HTML(string=html).write_pdf(output_path)
