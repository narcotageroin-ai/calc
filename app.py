
import io
import math
from typing import Optional

import pandas as pd
import streamlit as st


st.set_page_config(
    page_title="Калькулятор цен для маркетплейсов",
    layout="wide"
)

st.title("🧮 Калькулятор цен для маркетплейсов")

st.markdown(
    """
Программа считает рекомендуемую **цену продажи** и **маржинальность** по каждому товару.

1. Загружаем Excel с колонками: закупочная цена, ширина, высота, глубина (в сантиметрах).
2. Настраиваем параметры (НДС, комиссия МП, наценка, логистика, СПП и т.д.).
3. Получаем рассчитанные цену продажи, НДС и % маржинальности по каждому SKU и можем скачать новый Excel.
    """
)


@st.cache_data
def load_excel(file) -> pd.DataFrame:
    df = pd.read_excel(file)
    df.columns = df.columns.map(str)  # важно!
    return df



def guess_column(columns, keywords):
    """Пытаемся угадать колонку по ключевым словам (без учета регистра)."""
    # Гарантируем, что все имена колонок — строки
    columns = [str(c) for c in columns]
    cols_lower = [c.lower() for c in columns]

    for kw in keywords:
        kw = kw.lower()
        for i, c in enumerate(cols_lower):
            if kw in c:
                return i
    return 0



def calc_for_row(
    row,
    col_purchase: str,
    col_width: str,
    col_height: str,
    col_depth: str,
    vat_sale_rate: float,
    commission_pct: float,
    acquiring_pct: float,
    markup_pct: float,
    logistics_base_per_liter: float,
    logistics_extra_per_liter: float,
    packaging_cost: float,
    spp_pct: float,
    purchase_vat_rate: float = 20.0,
    min_margin_pct: float = 10.0,
) -> dict:
    """Выполняет расчет для одной строки DataFrame. Возвращает словарь с результатами.

    Все проценты (НДС, комиссии, маржа) передаются как % (например, 25 для 25%).
    """
    purchase_price = float(row[col_purchase]) if pd.notnull(row[col_purchase]) else 0.0
    width = float(row[col_width]) if pd.notnull(row[col_width]) else 0.0
    height = float(row[col_height]) if pd.notnull(row[col_height]) else 0.0
    depth = float(row[col_depth]) if pd.notnull(row[col_depth]) else 0.0

    # 1. Объем в литрах (из см³)
    volume_liters = (width * height * depth) / 1000.0

    # 2. Стоимость логистики:
    #    стоимость за первый литр + за каждый дополнительный литр.
    #    Используем "плавную" формулу, которая совпадает с примером при целых литрах.
    extra_liters = max(volume_liters - 1.0, 0.0)
    logistics_cost = logistics_base_per_liter + logistics_extra_per_liter * extra_liters if volume_liters > 0 else 0.0

    # 3. Наценка "от обратного":
    #    хотим, чтобы закупка была (1 - markup_pct) от цены без учета логистики/упаковки.
    #    Например, при 25% маржи закупка = 75% цены, => цена = закупка / 0.75.
    markup_factor = 1.0 - markup_pct / 100.0 if markup_pct < 100 else 0.0001
    base_price_with_markup = purchase_price / markup_factor if markup_factor > 0 else purchase_price

    # 4. Добавляем логистику и упаковку
    total_non_commission_cost = base_price_with_markup + logistics_cost + packaging_cost

    # 5. Учитываем комиссию МП и эквайринг (процент от цены)
    commission_total_pct = commission_pct + acquiring_pct
    denom = 1.0 - commission_total_pct / 100.0
    if denom <= 0:
        sale_price_initial = total_non_commission_cost
    else:
        sale_price_initial = total_non_commission_cost / denom

    # 6. Вычисляем НДС и маржу при данной цене,
    #    и при необходимости корректируем цену так, чтобы маржа была не менее min_margin_pct.

    # Входящий НДС с закупки (предполагаем, что закупочная цена с НДС)
    purchase_vat = purchase_price * purchase_vat_rate / (100.0 + purchase_vat_rate)

    def compute_profit_and_margin(price: float):
        # Комиссия и эквайринг
        commission_cost = price * commission_pct / 100.0
        acquiring_cost = price * acquiring_pct / 100.0

        # Исходящий НДС по ставке vat_sale_rate, с учетом СПП (co-invest)
        # Общая логика: НДС считается с цены после учета СПП.
        if vat_sale_rate > 0:
            outgoing_vat = price * (1.0 - spp_pct / 100.0) * vat_sale_rate / (100.0 + vat_sale_rate)
        else:
            outgoing_vat = 0.0

        vat_to_pay = outgoing_vat - purchase_vat

        profit = (
            price
            - purchase_price
            - logistics_cost
            - packaging_cost
            - commission_cost
            - acquiring_cost
            - vat_to_pay
        )

        margin_pct = (profit / price * 100.0) if price > 0 else 0.0
        return profit, margin_pct, outgoing_vat, vat_to_pay, commission_cost, acquiring_cost

    # Считаем для исходной цены
    profit_initial, margin_initial, outgoing_vat_initial, vat_to_pay_initial, commission_cost_initial, acquiring_cost_initial = compute_profit_and_margin(
        sale_price_initial
    )

    # Если маржа >= минимальной — оставляем эту цену
    if margin_initial >= min_margin_pct:
        sale_price_final = sale_price_initial
        profit_final = profit_initial
        margin_final = margin_initial
        outgoing_vat_final = outgoing_vat_initial
        vat_to_pay_final = vat_to_pay_initial
        commission_cost_final = commission_cost_initial
        acquiring_cost_final = acquiring_cost_initial
    else:
        # Решаем задачу аналитически: найти цену, при которой маржа = min_margin_pct
        # Пусть p — цена.
        # profit(p) = p - c - l - u - p*k - p*a - VAT(p)
        # где VAT(p) = p*(1 - spp)*v/(100+v) - purchase_vat
        # profit(p) = p * [1 - k - a - (1 - spp)*v/(100+v)] - (c + l + u - purchase_vat)
        # Маржа m = profit(p) / p.
        # m = A - B / p, где
        # A = 1 - k - a - (1 - spp)*v/(100+v),
        # B = c + l + u - purchase_vat.
        # Тогда для m = min_margin: p = B / (A - m).
        k = commission_pct / 100.0
        a = acquiring_pct / 100.0
        v = vat_sale_rate
        s = spp_pct / 100.0
        m = min_margin_pct / 100.0

        A = 1.0 - k - a - ((1.0 - s) * v / (100.0 + v) if v > 0 else 0.0)
        B = purchase_price + logistics_cost + packaging_cost - purchase_vat

        if A <= m:
            # Теоретически невозможно достичь такую маржу при разумной цене,
            # поэтому просто используем исходную цену и маржу.
            sale_price_final = sale_price_initial
            profit_final = profit_initial
            margin_final = margin_initial
            outgoing_vat_final = outgoing_vat_initial
            vat_to_pay_final = vat_to_pay_initial
            commission_cost_final = commission_cost_initial
            acquiring_cost_final = acquiring_cost_initial
        else:
            price_for_min_margin = B / (A - m)
            # На всякий случай не даем цене стать ниже исходной
            sale_price_final = max(sale_price_initial, price_for_min_margin)

            profit_final, margin_final, outgoing_vat_final, vat_to_pay_final, commission_cost_final, acquiring_cost_final = compute_profit_and_margin(
                sale_price_final
            )

    return {
        "Объем, л": volume_liters,
        "Стоимость логистики": logistics_cost,
        "Цена продажи": sale_price_final,
        "% маржи": margin_final,
        "Исходящий НДС": outgoing_vat_final,
        "Входящий НДС": purchase_vat,
        "НДС к уплате": vat_to_pay_final,
    }


uploaded_file = st.file_uploader("Загрузите Excel-файл с товарами", type=["xlsx"])

if uploaded_file is not None:
    df = load_excel(uploaded_file)
    st.subheader("Предпросмотр данных")
    st.dataframe(df.head())

    st.markdown("### Настройка колонок")
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        purchase_col = st.selectbox(
            "Колонка с **закупочной ценой**",
            options=df.columns.tolist(),
            index=guess_column(df.columns, ["закуп", "purchase", "cost"]),
        )
    with col2:
        width_col = st.selectbox(
            "Колонка с **шириной, см**",
            options=df.columns.tolist(),
            index=guess_column(df.columns, ["шир", "width"]),
        )
    with col3:
        height_col = st.selectbox(
            "Колонка с **высотой, см**",
            options=df.columns.tolist(),
            index=guess_column(df.columns, ["выс", "height"]),
        )
    with col4:
        depth_col = st.selectbox(
            "Колонка с **глубиной, см**",
            options=df.columns.tolist(),
            index=guess_column(df.columns, ["глуб", "depth", "длин"]),
        )

    st.markdown("### Параметры расчета")

    with st.expander("Налоги и комиссии", expanded=True):
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            vat_sale_rate = st.number_input("Ставка НДС на продажу, %", min_value=0.0, max_value=50.0, value=22.0, step=1.0)
        with col_b:
            commission_pct = st.number_input("Комиссия маркетплейса, %", min_value=0.0, max_value=100.0, value=15.0, step=0.1)
        with col_c:
            acquiring_pct = st.number_input("Эквайринг, %", min_value=0.0, max_value=100.0, value=1.8, step=0.1)

    with st.expander("Маржа и СПП", expanded=True):
        col_d, col_e = st.columns(2)
        with col_d:
            markup_pct = st.number_input("Желаемая наценка на товар, %", min_value=0.0, max_value=95.0, value=25.0, step=1.0)
        with col_e:
            spp_pct = st.number_input("СПП (соинвест МП), %", min_value=0.0, max_value=100.0, value=10.0, step=1.0)

    with st.expander("Логистика и упаковка", expanded=True):
        col_f, col_g, col_h = st.columns(3)
        with col_f:
            logistics_base_per_liter = st.number_input(
                "Стоимость 1 литра логистики, ₽",
                min_value=0.0,
                value=20.0,
                step=1.0,
            )
        with col_g:
            logistics_extra_per_liter = st.number_input(
                "Стоимость каждого доп. литра, ₽",
                min_value=0.0,
                value=10.0,
                step=1.0,
            )
        with col_h:
            packaging_cost = st.number_input(
                "Стоимость упаковки на складе, ₽",
                min_value=0.0,
                value=36.0,
                step=1.0,
            )

    min_margin_pct = 10.0
    st.info(f"Минимальная целевая маржа после всех затрат и НДС: **{min_margin_pct:.0f}%**")

    if st.button("🔢 Рассчитать цены"):
        # Приводим числовые столбцы к float (если там текст/строки)
        for c in [purchase_col, width_col, height_col, depth_col]:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        results = []
        for idx, row in df.iterrows():
            res = calc_for_row(
                row=row,
                col_purchase=purchase_col,
                col_width=width_col,
                col_height=height_col,
                col_depth=depth_col,
                vat_sale_rate=vat_sale_rate,
                commission_pct=commission_pct,
                acquiring_pct=acquiring_pct,
                markup_pct=markup_pct,
                logistics_base_per_liter=logistics_base_per_liter,
                logistics_extra_per_liter=logistics_extra_per_liter,
                packaging_cost=packaging_cost,
                spp_pct=spp_pct,
            )
            results.append(res)

        res_df = pd.DataFrame(results)

        final_df = pd.concat([df.reset_index(drop=True), res_df], axis=1)

        st.markdown("### Результаты расчета")
        st.dataframe(final_df)

        # Подготовка Excel для скачивания
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
            final_df.to_excel(writer, index=False, sheet_name="Расчет")
        output.seek(0)

        st.download_button(
            label="⬇️ Скачать Excel с расчетами",
            data=output,
            file_name="pricing_calculation.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        st.success("Расчет выполнен. Файл готов к загрузке.")
else:
    st.info("Загрузите Excel-файл, чтобы выполнить расчет.")
