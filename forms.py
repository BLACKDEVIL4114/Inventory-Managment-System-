from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, SelectField, IntegerField
from wtforms.validators import DataRequired, Email, EqualTo, ValidationError, Optional, Length
from models import User

class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Login')

    def validate_email(self, email):
        if not email.data.lower().endswith('@gmail.com'):
            raise ValidationError('Only @gmail.com addresses are allowed.')

class RegistrationForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password')])
    role = SelectField('Role', choices=[('staff', 'Warehouse Staff'), ('manager', 'Inventory Manager')], default='staff')
    submit = SubmitField('Sign Up')

    def validate_username(self, username):
        user = User.query.filter_by(username=username.data).first()
        if user:
            raise ValidationError('That username is taken. Please choose a different one.')

    def validate_email(self, email):
        if not email.data.lower().endswith('@gmail.com'):
            raise ValidationError('Only @gmail.com addresses are allowed.')
        user = User.query.filter_by(email=email.data).first()
        if user:
            raise ValidationError('That email is taken. Please choose a different one.')

class ProductForm(FlaskForm):
    name = StringField('Product Name', validators=[DataRequired()])
    sku = StringField('SKU', validators=[DataRequired()])
    category = StringField('Category')
    unit = StringField('Unit (e.g. kg, piece)')
    unit_price = IntegerField('Unit Price (Selling)', default=0)
    cost_price = IntegerField('Cost Price', default=0)
    min_stock_level = IntegerField('Minimum Stock Level', default=10)
    warehouse_id = SelectField('Primary Warehouse', coerce=int, validators=[Optional()])
    submit = SubmitField('Save Product')

class WarehouseForm(FlaskForm):
    name = StringField('Warehouse Name', validators=[DataRequired()])
    location = StringField('Location')
    submit = SubmitField('Save Warehouse')

class ForgotPasswordForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    submit = SubmitField('Request OTP')

    def validate_email(self, email):
        if not email.data.lower().endswith('@gmail.com'):
            raise ValidationError('Only @gmail.com addresses are allowed.')

class ResetPasswordForm(FlaskForm):
    otp = StringField('OTP', validators=[DataRequired()])
    password = PasswordField('New Password', validators=[DataRequired()])
    confirm_password = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField('Reset Password')

class UpdateProfileForm(FlaskForm):
    username         = StringField('Username', validators=[DataRequired(), Length(min=2, max=80)])
    email            = StringField('Email', validators=[DataRequired(), Email()])
    new_password     = PasswordField('New Password', validators=[Optional(), Length(min=6)])
    confirm_password = PasswordField('Confirm New Password',
                           validators=[Optional(), EqualTo('new_password', message='Passwords must match')])
    submit           = SubmitField('Save Changes')

    def __init__(self, original_username, original_email, *args, **kwargs):
        super(UpdateProfileForm, self).__init__(*args, **kwargs)
        self.original_username = original_username
        self.original_email    = original_email

    def validate_username(self, username):
        if username.data != self.original_username:
            user = User.query.filter_by(username=username.data).first()
            if user:
                raise ValidationError('That username is already taken.')

    def validate_email(self, email):
        if not email.data.lower().endswith('@gmail.com'):
            raise ValidationError('Only @gmail.com addresses are allowed.')
        if email.data != self.original_email:
            user = User.query.filter_by(email=email.data).first()
            if user:
                raise ValidationError('That email is already registered.')
