<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">

  <style>
    @import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700;900&display=swap');

    @page {
      size: letter;
      margin: 0.55in 0.6in;
    }

    body {
      font-family: 'Roboto', Arial, sans-serif;
      line-height: 1.28;
      font-size: 12.5px;
      color: #111;
      margin: 0;
    }

    .header {
      border-bottom: 3px solid {{ party_color }};
      padding-bottom: 12px;
      margin-bottom: 14px;
      overflow: auto;
      break-inside: avoid;
      page-break-inside: avoid;
    }

    .photo {
      float: right;
      width: 112px;
      height: auto;
      border-radius: 6px;
      margin-left: 18px;
      object-fit: cover;
    }

    .name {
      font-size: 27px;
      font-weight: 900;
      color: {{ party_color }};
      margin-bottom: 3px;
      line-height: 1.05;
    }

    .subhead {
      font-size: 14.5px;
      font-weight: 500;
      margin-bottom: 3px;
    }

    .meta-line {
      font-size: 12.5px;
      margin-bottom: 2px;
      font-weight: 500;
    }

    .section {
      margin-top: 12px;
      break-inside: avoid;
      page-break-inside: avoid;
    }

    .section-title {
      font-size: 15px;
      font-weight: 700;
      margin-bottom: 5px;
      break-after: avoid;
      page-break-after: avoid;
    }

    .bullet {
      margin-left: 13px;
      margin-bottom: 3px;
      break-inside: avoid;
      page-break-inside: avoid;
    }

    p {
      margin: 0 0 4px 0;
    }

    .positioning-line {
      font-weight: 700;
      margin-bottom: 4px;
    }
  </style>
</head>

<body>

<div class="header">
  <img class="photo" src="{{ image_url }}">
  <div class="name">{{ name }}</div>
  <div class="subhead">{{ chamber }} District {{ district }}</div>

  {% if party_label or location_line %}
  <div class="meta-line">
    {% if party_label %}{{ party_label }}{% endif %}{% if party_label and location_line %} | {% endif %}{% if location_line %}{{ location_line }}{% endif %}
  </div>
  {% endif %}
</div>

<div class="section">
  <div class="section-title">Committee Relevance</div>
  <p>{{ committee }}</p>
</div>

<div class="section">
  <div class="section-title">Time in Office</div>
  {% for item in time_in_office %}
  <div class="bullet">• {{ item }}</div>
  {% endfor %}
</div>

<div class="section">
  <div class="section-title">Biography</div>
  {% for item in bio %}
  <div class="bullet">• {{ item }}</div>
  {% endfor %}
</div>

<div class="section">
  <div class="section-title">Key Issues</div>
  {% for item in issues %}
  <div class="bullet">• {{ item }}</div>
  {% endfor %}
</div>

<div class="section">
  <div class="section-title">District Signals</div>
  {% for item in district_signals %}
  <div class="bullet">• {{ item }}</div>
  {% endfor %}
</div>

<div class="section">
  <div class="section-title">Legislative Focus</div>
  {% for item in focus %}
  <div class="bullet">• {{ item }}</div>
  {% endfor %}
</div>

<div class="section">
  <div class="section-title">Key Bills</div>
  {% for item in bills %}
  <div class="bullet">• {{ item }}</div>
  {% endfor %}
</div>

<div class="section">
  <div class="section-title">Political Positioning</div>
  <div class="positioning-line">{{ positioning }}</div>
  {% for item in positioning_notes %}
  <div class="bullet">• {{ item }}</div>
  {% endfor %}
</div>

<div class="section">
  <div class="section-title">SBDC Framing</div>
  <p>{{ sbdc }}</p>
</div>

<div class="section">
  <div class="section-title">Talking Points</div>
  {% for item in talking %}
  <div class="bullet">• {{ item }}</div>
  {% endfor %}
</div>

</body>
</html>
