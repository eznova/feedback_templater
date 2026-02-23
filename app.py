from flask import Flask, render_template, request
from datetime import datetime

app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return render_template("form.html")


@app.route("/generate", methods=["POST"])
def generate():
    meeting_name=request.form.get("meeting_name")
    if meeting_name.lower() == "запрос":
        main_color_light = "9FDEDE"
        main_color_dark = "28D7D7"
        factors_bg_color = "B3E4E4"
    else:
        main_color_light = "AB83F5"
        main_color_dark = "844BEC"
        factors_bg_color = "E4D5FF"
    
    # Дата
    raw_date = request.form.get("meeting_date")
    formatted_date = ""
    if raw_date:
        date_obj = datetime.strptime(raw_date, "%Y-%m-%d")
        formatted_date = date_obj.strftime("%d / %m")

    # Значимые факторы — разбиваем по строкам
    raw_factors = request.form.get("factors", "")
    factors_list = [
        line.strip()
        for line in raw_factors.splitlines()
        if line.strip()
    ]

    # Общие впечатления — разбиваем по строкам
    raw_impressions = request.form.get("impressions", "")
    impressions_list = [
        line.strip()
        for line in raw_impressions.splitlines()
        if line.strip()
    ]

    # Мотиваторы — разбиваем по строкам
    raw_motivators = request.form.get("motivators", "")
    motivators_list = [
        line.strip()
        for line in raw_motivators.splitlines()
        if line.strip()
    ]

    # Области внимания — разбиваем по строкам
    raw_attention = request.form.get("attention", "")
    attention_list = [
        line.strip()
        for line in raw_attention.splitlines()
        if line.strip()
    ]

    # Цвет блока с рисками
    risk_value = request.form.get("risks", "")
    if risk_value.lower() == "обрати внимание":
        risk_color = "FFC72E"
    elif risk_value.lower() == "есть риски":
        risk_color = "B93131"
    else:
        risk_color = "76B48F"

    return render_template(
        "result.html",
        meeting_name=meeting_name,
        main_color_light=main_color_light,
        main_color_dark=main_color_dark,
        factors_bg_color=factors_bg_color,
        meeting_date=formatted_date,
        risks=risk_value,
        risk_color=risk_color,            # ← передаём цвет
        fio=request.form.get("fio"),
        impressions_list=impressions_list,
        motivators_list=motivators_list,
        factors_list=factors_list,
        attention_list=attention_list
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)