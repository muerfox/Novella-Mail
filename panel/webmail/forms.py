from django import forms
from django.core.validators import RegexValidator

_domain_validator = RegexValidator(
    regex=r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$",
    message="Enter a valid domain name, e.g. example.com",
)
_local_part_validator = RegexValidator(
    regex=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$",
    message="Use letters, numbers, and . _ + - only.",
)


class LoginForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "username", "placeholder": "you@example.com"})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"})
    )


class ComposeForm(forms.Form):
    to = forms.CharField(
        label="To",
        widget=forms.TextInput(attrs={"placeholder": "recipient@example.com, another@example.com"}),
    )
    cc = forms.CharField(label="Cc", required=False)
    subject = forms.CharField(required=False, widget=forms.TextInput(attrs={"placeholder": "(no subject)"}))
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 14}), required=False)
    in_reply_to = forms.CharField(required=False, widget=forms.HiddenInput())
    references = forms.CharField(required=False, widget=forms.HiddenInput())


class AddDomainForm(forms.Form):
    domain = forms.CharField(
        validators=[_domain_validator],
        widget=forms.TextInput(attrs={"placeholder": "example.com", "autofocus": True}),
    )
    description = forms.CharField(required=False, widget=forms.TextInput())

    def clean_domain(self):
        return self.cleaned_data["domain"].strip().lower()


class AddMailboxForm(forms.Form):
    local_part = forms.CharField(
        label="Mailbox",
        validators=[_local_part_validator],
        widget=forms.TextInput(attrs={"placeholder": "info"}),
    )
    name = forms.CharField(label="Display name", required=False)
    password = forms.CharField(widget=forms.PasswordInput())
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput())

    def clean_local_part(self):
        return self.cleaned_data["local_part"].strip().lower()

    def clean(self):
        cleaned = super().clean()
        pw, pw2 = cleaned.get("password"), cleaned.get("password2")
        if pw and pw2 and pw != pw2:
            self.add_error("password2", "Passwords don't match.")
        if pw and len(pw) < 8:
            self.add_error("password", "Use at least 8 characters.")
        return cleaned
