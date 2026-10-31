from flask_wtf import FlaskForm
from wtforms import (
    EmailField,
    HiddenField,
    IntegerField,
    PasswordField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Email, InputRequired, Length, NumberRange


def strip_text(value):
    return value.strip() if value is not None else value


class LoginForm(FlaskForm):
    username = StringField(
        "Username", filters=[strip_text],
        validators=[DataRequired(), Length(max=40)],
    )
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Sign in")


class RegisterForm(FlaskForm):
    username = StringField(
        "Username", filters=[strip_text],
        validators=[DataRequired(), Length(min=3, max=40)],
    )
    email = EmailField(
        "Email", filters=[strip_text],
        validators=[DataRequired(), Email(), Length(max=120)],
    )
    password = PasswordField(
        "Password", validators=[DataRequired(), Length(min=10, max=128)]
    )
    submit = SubmitField("Create account")


class QuantityForm(FlaskForm):
    quantity = IntegerField(
        "Quantity", validators=[InputRequired(), NumberRange(min=1, max=50)]
    )


class CheckoutForm(FlaskForm):
    checkout_key = HiddenField(validators=[DataRequired(), Length(min=32, max=32)])
    full_name = StringField(
        "Full name", filters=[strip_text],
        validators=[DataRequired(), Length(min=2, max=100)],
    )
    email = EmailField(
        "Contact email", filters=[strip_text],
        validators=[DataRequired(), Email(), Length(max=120)],
    )
    address = TextAreaField(
        "Street address", filters=[strip_text],
        validators=[DataRequired(), Length(min=5, max=400)],
    )
    city = StringField(
        "City", filters=[strip_text],
        validators=[DataRequired(), Length(min=2, max=80)],
    )
    postal_code = StringField(
        "Postal code", filters=[strip_text],
        validators=[DataRequired(), Length(min=2, max=20)],
    )
    country = StringField(
        "Country", filters=[strip_text],
        validators=[DataRequired(), Length(min=2, max=80)],
    )
    submit = SubmitField("Place demo order")
