from flask import Flask, render_template, request
from datetime import datetime

app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return render_template("form.html")


@app.route("/generate", methods=["POST"])
def generate():
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

    return render_template(
        "result.html",
        meeting_name=request.form.get("meeting_name"),
        meeting_date=formatted_date,
        risks=request.form.get("risks"),
        fio=request.form.get("fio"),
        impressions_list=impressions_list,  # ← список строк
        motivators=request.form.get("motivators"),
        factors_list=factors_list,          # список строк
        attention=request.form.get("attention"),
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)