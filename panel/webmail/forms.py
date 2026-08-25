"""Django forms used by webmail/views.py - one class per HTML form in the app."""
from django import forms
from django.core.validators import RegexValidator

# Domain names: labels of 1-63 chars, alphanumeric/hyphen, no leading or
# trailing hyphen in a label, at least one dot (so "localhost" alone is
# rejected but "mail.localhost" would pass - deliberately permissive about
# what counts as a "real" TLD, since anyone can run their own DNS).
_domain_validator = RegexValidator(
    regex=r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$",
    message="Enter a valid domain name, e.g. example.com",
)
# The part of an email address before the @ - kept intentionally simple
# (not the full, much stranger RFC 5321 local-part grammar) since this only
# needs to catch obviously-wrong input; docker-mailserver/Postfix are the
# real authority on what's a valid address.
_local_part_validator = RegexValidator(
    regex=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$",
    message="Use letters, numbers, and . _ + - only.",
)


class LoginForm(forms.Form):
    """The only "account" form in the app - see webmail.views.login_view: there's no separate user database."""
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "username", "placeholder": "you@example.com"})
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"})
    )


class ComposeForm(forms.Form):
    """New message / reply / forward - see webmail.views.compose_view for how `initial` gets pre-filled for replies."""
    to = forms.CharField(
        label="To",
        widget=forms.TextInput(attrs={"placeholder": "recipient@example.com, another@example.com"}),
    )
    cc = forms.CharField(label="Cc", required=False)
    subject = forms.CharField(required=False, widget=forms.TextInput(attrs={"placeholder": "(no subject)"}))
    body = forms.CharField(widget=forms.Textarea(attrs={"rows": 14}), required=False)
    # Carried through hidden fields rather than re-derived on submit, so a
    # reply still threads correctly (In-Reply-To/References headers) even
    # if the user edits the subject line before sending.
    in_reply_to = forms.CharField(required=False, widget=forms.HiddenInput())
    references = forms.CharField(required=False, widget=forms.HiddenInput())


class AddDomainForm(forms.Form):
    # docker-mailserver has no domain-registration step - domains are
    # derived from whatever addresses exist - so this form just validates
    # the typed name before sending you to that domain's admin page.
    domain = forms.CharField(
        validators=[_domain_validator],
        widget=forms.TextInput(attrs={"placeholder": "example.com", "autofocus": True}),
    )

    def clean_domain(self):
        return self.cleaned_data["domain"].strip().lower()


class AddMailboxForm(forms.Form):
    """Used on a domain's admin page to create a new mailbox@that-domain."""
    local_part = forms.CharField(
        label="Mailbox",
        validators=[_local_part_validator],
        widget=forms.TextInput(attrs={"placeholder": "info"}),
    )
    password = forms.CharField(widget=forms.PasswordInput())
    password2 = forms.CharField(label="Confirm password", widget=forms.PasswordInput())

    def clean_local_part(self):
        return self.cleaned_data["local_part"].strip().lower()

    def clean(self):
        # Cross-field validation (comparing two fields to each other) has
        # to happen here in clean(), not in a per-field clean_<name>()
        # method, since it needs both password fields' cleaned values at
        # once.
        cleaned = super().clean()
        pw, pw2 = cleaned.get("password"), cleaned.get("password2")
        if pw and pw2 and pw != pw2:
            self.add_error("password2", "Passwords don't match.")
        if pw and len(pw) < 8:
            self.add_error("password", "Use at least 8 characters.")
        return cleaned
