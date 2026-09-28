"""Demandes RH : droits du demandeur, workflow, documents et notifications."""

import base64
from uuid import UUID
from psycopg2.errors import UniqueViolation, SerializationFailure
from markupsafe import Markup
from odoo.tools import file_open
from odoo.tools.image import image_data_uri

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError


class HrRequest(models.Model):
    _name = 'hr.request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = 'HR Requests'

    name = fields.Char(
        string='Reference',
        required=True,
        copy=False,
        readonly=True,
        default=lambda self: self.env._('New Request')
    )

    request_type_id = fields.Many2one(
        'hr.request.type',
        string='Request Type',
        required=True,
        tracking=True
    )

    can_manage = fields.Boolean(compute='_compute_can_manage', string='Can Manage')
    portal_submission_token = fields.Char(copy=False, readonly=True)
    _sql_constraints = [
        ('portal_submission_unique', 'unique(create_uid, portal_submission_token)',
         'This form has already been submitted.'),
    ]

    @api.model
    def _create_portal_submission(self, token, vals):
        """Créer et soumettre une seule demande par jeton et utilisateur.

        La contrainte SQL arbitre les POST simultanés. Une collision sur cette
        contrainte déclenche une nouvelle transaction via le mécanisme de retry Odoo.
        """
        try:
            token = str(UUID(token or ''))
        except (ValueError, TypeError, AttributeError):
            raise UserError(self.env._('Please reload the form and try again.'))
        existing = self.search([
            ('create_uid', '=', self.env.uid), ('portal_submission_token', '=', token),
        ], limit=1)
        if existing:
            return existing
        try:
            with self.env.cr.savepoint():
                record = self.create(dict(vals, portal_submission_token=token))
                record.action_submit()
                return record
        except UniqueViolation as error:
            if error.diag.constraint_name != 'hr_request_portal_submission_unique':
                raise
            # Odoo retries serialization failures in a fresh transaction/snapshot.
            raise SerializationFailure('Concurrent portal submission; retry') from error
    state = fields.Selection([
        ('draft', 'Draft'), ('submitted', 'Submitted'),
        ('signed', 'Signed'), ('done', 'Done'), ('rejected', 'Rejected'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)
    signed_document_id = fields.Many2one('ir.attachment', string='Signed Document', readonly=True, copy=False)
    signed_date = fields.Datetime(string='Signed On', readonly=True, copy=False)
    signed_by_id = fields.Many2one('res.users', string='Signature Confirmed By', readonly=True, copy=False)
    rejection_reason = fields.Text(string='Rejection Reason', readonly=True, copy=False)
    rejected_by_id = fields.Many2one('res.users', string='Rejected By', readonly=True, copy=False)
    rejected_date = fields.Datetime(string='Rejected On', readonly=True, copy=False)

    # Requester
    requester_employee_id = fields.Many2one(
        'hr.employee',
        string='Requester',
        default=lambda self: self._default_requester_employee(),
        tracking=True
    )

    # La société vient de la fiche employé ; elle n’est pas saisie une seconde fois.
    company_id = fields.Many2one(
        related='requester_employee_id.company_id', string='Company',
        store=True, readonly=True,
    )

    department_id = fields.Many2one(
        'hr.department',
        string='Department',
        compute='_compute_department',
        store=True
    )

    submitted_date = fields.Datetime(
        string='Submitted on',
        readonly=True
    )

    description = fields.Text(
        string='Description'
    )

    generated_document_id = fields.Many2one(
        'ir.attachment',
        string='Generated Document',
        readonly=True
    )

    generated_date = fields.Datetime(
        string='Generated On',
        readonly=True
    )

    # Current employee
    def _get_employee_for_user(self, user=None, company=None, strict=True):
        """Résoudre une fiche unique sans choisir arbitrairement un employé.

        Les internes utilisent la société active, ou celle explicitement fournie.
        Le portail reste lié au contact professionnel, indépendamment du sélecteur
        de sociétés. Les notifications peuvent demander une résolution non stricte
        pour utiliser l'adresse du contact si la fiche est absente ou ambiguë.
        """
        user = user or self.env.user
        Employee = self.env['hr.employee'].sudo()
        portal = user.has_group('base.group_portal')
        if portal:
            domain = [('work_contact_id', '=', user.partner_id.id)]
        else:
            company = company or self.env.company
            if company.id not in user.company_ids.ids:
                if strict:
                    raise UserError(self.env._(
                        'The selected company is not allowed for this user.'
                    ))
                return Employee.browse()
            domain = [('user_id', '=', user.id), ('company_id', '=', company.id)]
        employees = Employee.search(domain, limit=2)
        if len(employees) > 1:
            if strict:
                raise UserError(self.env._(
                    'Several employee profiles match your account. Ask HR to resolve this ambiguity.'
                ))
            return Employee.browse()
        if not employees and not portal and strict:
            raise UserError(self.env._(
                'No employee is linked to your account in the selected company. Contact HR.'
            ))
        return employees

    def _get_hr_assistance_employees(self, user=None):
        """Réunir l’employé du compte et les employés qui lui sont affectés."""
        user = user or self.env.user
        own_employee = self._get_employee_for_user(user, strict=False)
        if not user.has_group('hr_requests.group_hr_request_hr_assistance'):
            return own_employee
        return (own_employee | user.sudo().managed_employee_ids).sorted('name')

    # Default requester
    def _default_requester_employee(self):
        return self._get_employee_for_user()

    # Requester access
    def _check_requester_access(self, requester_employee):
        """Vérifier le demandeur côté serveur, indépendamment des domaines des vues.

        HR Assistance hérite du portail : son cas doit être traité avant celui
        d’un compte portail ordinaire, limité à ses propres demandes.
        """
        current_user = self.env.user
        own_employee = self._get_employee_for_user(
            current_user,
            strict=not current_user.has_group('hr_requests.group_hr_request_hr_assistance'),
        )
        if not requester_employee.exists():
            raise UserError(self.env._('Please select a valid employee.'))

        if current_user.has_group(
            'hr_requests.group_hr_request_hr_assistance'
        ):
            if requester_employee in self._get_hr_assistance_employees(current_user):
                return

            raise UserError(
                self.env._('You can only select yourself or an employee assigned to you.')
            )

        if current_user.has_group('base.group_portal'):
            if requester_employee != own_employee:
                raise UserError(
                    self.env._('You can only create requests for yourself.')
                )
            return

        if requester_employee != own_employee:
            raise UserError(self.env._('You can only select yourself as requester.'))

    # Create
    @api.model_create_multi
    def create(self, vals_list):
        """Initialiser une demande en brouillon et refuser les valeurs de workflow entrantes."""
        current_user = self.env.user
        own_employee = self._get_employee_for_user(
            current_user,
            strict=not current_user.has_group('hr_requests.group_hr_request_hr_assistance'),
        )

        for vals in vals_list:
            vals.pop('company_id', None)
            # Never accept workflow values from a form, RPC or context defaults.
            vals.update({
                'state': 'draft', 'submitted_date': False,
                'signed_document_id': False, 'signed_date': False, 'signed_by_id': False,
                'rejection_reason': False, 'rejected_by_id': False, 'rejected_date': False,
                'generated_document_id': False, 'generated_date': False,
                'name': self.env.ref('hr_requests.sequence_hr_request').sudo().next_by_id(),
            })

            requester_employee_id = vals.get(
                'requester_employee_id'
            )

            if not requester_employee_id:
                if own_employee:
                    requester_employee_id = own_employee.id
                    vals['requester_employee_id'] = own_employee.id
                else:
                    raise UserError(
                        self.env._('No employee record is linked to your user account.')
                    )

            requester_employee = self.env[
                'hr.employee'
            ].browse(requester_employee_id)

            self._check_requester_access(
                requester_employee
            )

        return super().create(vals_list)

    # Write rules
    def write(self, vals):
        """Réserver les champs de workflow aux actions et les informations au brouillon."""
        protected_fields = {
            'company_id',
            'portal_submission_token',
            'name',
            'state',
            'submitted_date',
            'generated_document_id',
            'generated_date', 'signed_document_id', 'signed_date', 'signed_by_id',
            'rejection_reason', 'rejected_by_id', 'rejected_date',
        }

        if protected_fields.intersection(vals):
            raise UserError(
                self.env._('These fields can only be changed by workflow actions.')
            )

        draft_fields = {
            'requester_employee_id',
            'request_type_id',
            'description',
        }

        if draft_fields.intersection(vals):
            for request in self:
                if request.state != 'draft':
                    raise UserError(
                        self.env._('Request information can only be changed in Draft.')
                    )

        if vals.get('requester_employee_id'):
            requester_employee = self.env['hr.employee'].browse(
                vals['requester_employee_id']
            )
            self._check_requester_access(requester_employee)

        return super().write(vals)

    def _workflow_write(self, vals):
        # Workflow changes use explicit notifications below. Disabling automatic
        # tracking here prevents a missing operator e-mail from aborting the
        # business action before our safe notification sender is applied.
        """Écrire les valeurs de workflow après les vérifications de l’action appelante.

        Cette méthode privée contourne volontairement la protection de write() ;
        elle ne remplace jamais un contrôle d’accès ou de transition.
        """
        result = super(HrRequest, self.with_context(tracking_disable=True)).write(vals)

        if vals.get('state') in ('signed', 'done', 'rejected'):
            for request in self:
                request._clear_validation_activities()

        return result

    # Department
    @api.depends('requester_employee_id')
    def _compute_department(self):
        for request in self:
            if request.requester_employee_id:
                request.department_id = (
                    request.requester_employee_id.department_id
                )
            else:
                request.department_id = False

    def _is_own_request(self, user):
        """Exclure de la validation le créateur et le bénéficiaire de la demande."""
        self.ensure_one()
        employee = self.requester_employee_id.sudo()
        return (self.create_uid == user or employee.user_id == user
                or employee.work_contact_id == user.partner_id)


    # Notifications
    def _notification_email_from(self):
        """Choisir le premier expéditeur renseigné, avec une valeur de repli.

        Cette sélection ne valide ni la syntaxe de l’adresse ni sa délivrabilité SMTP.
        """
        self.ensure_one()
        company = self.requester_employee_id.sudo().company_id or self.env.company
        candidates = [
            company.email,
            self.env.user.partner_id.email,
            self.env.company.email,
            self.env['ir.config_parameter'].sudo().get_param('mail.default.from'),
        ]
        return next((email.strip() for email in candidates if email and email.strip()),
                    'no-reply@example.com')

    def _post_note(self, body):
        """Publier une note interne avec un expéditeur explicite."""
        self.ensure_one()
        return self.message_post(
            body=body,
            message_type='comment',
            subtype_xmlid='mail.mt_note',
            email_from=self._notification_email_from(),
        )

    def _send_email(
        self,
        email_to,
        subject,
        body,
        attachment_ids=None
    ):
        """Créer un e-mail en file d’attente sans contacter le serveur SMTP ici.

        La création participe à la transaction courante ; l’envoi est effectué
        ultérieurement par la tâche planifiée de messagerie Odoo.
        """
        self.ensure_one()

        if not email_to:
            return

        email_from = self._notification_email_from()

        mail_values = {
            'subject': subject,
            'body_html': body,
            'email_from': email_from,
            'email_to': email_to,
        }

        if attachment_ids:
            mail_values['attachment_ids'] = [
                (6, 0, attachment_ids)
            ]

        # Odoo sends this after the business transaction has committed.
        self.env['mail.mail'].sudo().create(mail_values)
        return 'queued'

    def _get_user_email(self, user):
        employee = self._get_employee_for_user(
            user, company=self.requester_employee_id.sudo().company_id, strict=False,
        )

        if employee and employee.work_email:
            return employee.work_email

        return user.partner_id.email

    def _validation_activity_summary(self):
        self.ensure_one()
        return self.env._('Validate HR Request %(value1)s', value1=self.name)

    def _clear_validation_activities(self):
        """Supprimer les activités de validation reconnues dans les langues installées."""
        self.ensure_one()

        languages = self.env['res.lang'].sudo().search([]).mapped('code')
        summaries = {
            self.with_context(lang=lang)._validation_activity_summary()
            for lang in set(languages) | {'en_US'}
        }
        activities = self.env['mail.activity'].sudo().search([
            ('res_model', '=', self._name),
            ('res_id', '=', self.id),
            ('summary', 'in', list(summaries)),
        ])

        if activities:
            activities.unlink()

    def _notify_current_validators(self):
        """Créer les activités des gestionnaires éligibles et mettre leur e-mail en file."""
        self.ensure_one()

        validator_users = self._get_manager_users()

        if not validator_users:
            raise UserError(
                self.env._("No manager is configured for this employee's company.")
            )

        self._clear_validation_activities()

        activity_type = self.env.ref(
            'mail.mail_activity_data_todo'
        )

        model_id = self.env['ir.model']._get(
            self._name
        ).id

        validator_emails = []

        for user in validator_users:
            self.env['mail.activity'].sudo().create({
                'activity_type_id': activity_type.id,
                'res_model_id': model_id,
                'res_id': self.id,
                'user_id': user.id,
                'summary': self._validation_activity_summary(),
                'note': (
                    self.env._('The request %(value1)s is waiting for your validation.', value1=self.name)
                ),
                'date_deadline': fields.Date.context_today(self),
            })

            validator_email = self._get_user_email(user)

            if validator_email:
                validator_emails.append(validator_email)

        validator_emails = list(dict.fromkeys(validator_emails))

        if validator_emails:
            subject, html, _message = self._notification_fr('validation')
            self._send_email(', '.join(validator_emails), subject, html)

    def _document_label_fr(self):
        self.ensure_one()
        return {
            'work_certificate': 'Attestation de travail',
            'salary_certificate': 'Attestation de salaire',
            'credit_request': 'Demande de crédit',
        }.get(self.request_type_id._document_code(), self.request_type_id.name)

    def _report_logo_data_uri(self):
        """Utiliser le logo fourni pour Metraco et le logo de société pour les autres."""
        self.ensure_one()
        company = self.requester_employee_id.sudo().company_id
        if company.name.strip().casefold() != 'metraco':
            return image_data_uri(company.logo) if company.logo else ''
        with file_open('hr_requests/static/description/logo.jpg', 'rb') as logo:
            return 'data:image/jpeg;base64,' + base64.b64encode(logo.read()).decode('ascii')

    def _report_issue_date(self):
        return fields.Date.context_today(self).strftime('%d/%m/%Y')

    def _notification_fr(self, event):
        """Composer le message transactionnel français avec échappement des valeurs.

        Markup.format protège les données interpolées ; le motif de rejet ne doit
        pas être concaténé directement au HTML.
        """
        self.ensure_one()
        titles = {
            'rejected': 'Demande rejetée',
            'validation': 'Demande à traiter',
            'signed': 'Document signé',
            'done': 'Votre document signé est disponible',
        }
        messages = {
            'rejected': 'Votre demande a été rejetée. Le motif est indiqué ci-dessous.',
            'validation': 'Une demande de document a été soumise. Téléchargez le document, faites-le signer puis déposez le PDF signé dans Odoo.',
            'signed': 'Le document signé a été déposé. La demande est en cours de finalisation.',
            'done': 'Votre demande est terminée. Le document signé est disponible en pièce jointe et dans votre espace de suivi.',
        }
        title = titles.get(event, 'Mise à jour de votre demande')
        message = messages.get(event, 'Le suivi de la demande a été mis à jour.')
        # The request action has already checked access. Portal employees must not
        # receive general HR access merely to format a notification to managers.
        employee = self.requester_employee_id.sudo()
        company = employee.company_id or self.env.company
        reason_html = (Markup('<p style="padding:14px;background:#fff2f0;white-space:pre-wrap"><b>Motif du rejet :</b><br/>{}</p>').format(self.rejection_reason)
                       if event == 'rejected' else Markup(''))
        html = Markup('''<div style="background:#f2f5f8;padding:28px 12px;font-family:Arial,Helvetica,sans-serif;color:#243447">
          <table role="presentation" style="width:100%;max-width:640px;margin:0 auto;border-collapse:collapse;background:#ffffff">
            <tr><td style="padding:24px 30px;background:#16465b;color:white"><div style="font-size:13px;letter-spacing:1px">GESTION DES DOCUMENTS</div><div style="font-size:23px;font-weight:bold;margin-top:8px">{title}</div></td></tr>
            <tr><td style="padding:30px;font-size:15px;line-height:1.7"><p>Bonjour,</p><p>{message}</p>
              <table role="presentation" style="width:100%;border-collapse:collapse;background:#f4f7f9;font-size:14px">
                <tr><td style="padding:12px 16px;color:#526878">Référence</td><td style="padding:12px 16px;font-weight:bold">{reference}</td></tr>
                <tr><td style="padding:12px 16px;color:#526878">Employé concerné</td><td style="padding:12px 16px">{employee}</td></tr>
                <tr><td style="padding:12px 16px;color:#526878">Document</td><td style="padding:12px 16px">{document}</td></tr>
              </table>{reason}<p style="margin-top:24px">Cordialement,<br/><b>Service des ressources humaines</b><br/>{company}</p>
            </td></tr><tr><td style="padding:16px 30px;border-top:1px solid #e1e8ee;font-size:12px;color:#647889">Notification automatique · Conservez la référence pour faciliter le suivi de votre demande.</td></tr>
          </table></div>''').format(title=title,message=message,reference=self.name,
                employee=employee.name,document=self._document_label_fr(),company=company.name,reason=reason_html)
        return '%s — %s' % (title, self.name), html, message

    def _notify_requester(self, attachment_ids=None):
        """Notifier le bénéficiaire et ses assistants actifs, sans doublon d’adresse."""
        self.ensure_one()
        requester = self.requester_employee_id
        subject, html, message = self._notification_fr(self.state)
        self._post_note(message)
        recipients = []
        requester_email = requester.work_email or requester.work_contact_id.email or requester.user_id.partner_id.email
        if requester_email:
            recipients.append(requester_email)
        else:
            self._post_note('Notification non envoyée à l’employé : aucune adresse e-mail renseignée.')

        group = self.env.ref('hr_requests.group_hr_request_hr_assistance')
        assistants = self.env['res.users'].sudo().search([
            ('active', '=', True), ('groups_id', 'in', group.ids),
            ('managed_employee_ids', 'in', requester.ids),
        ])
        creator = self.create_uid.sudo()
        if not assistants and not (creator.has_group('hr_requests.group_hr_request_hr_assistance')
                and requester == self._get_employee_for_user(creator, strict=False)):
            self._post_note('Aucune copie envoyée à l’assistance RH : aucun assistant actif n’est affecté à cet employé. Vérifiez Configuration → Affecter les employés.')
        for assistant in assistants:
            # Portal login/contact is the primary notification address for an assistant.
            address = assistant.partner_id.email or self._get_user_email(assistant)
            if address:
                recipients.append(address)
            else:
                self._post_note('Copie non envoyée à %s : aucune adresse e-mail renseignée.' % assistant.name)
        seen = set()
        addresses = []
        for address in recipients:
            key = address.strip().lower()
            if key in seen:
                continue
            seen.add(key)
            addresses.append(address)
        if addresses:
            address_list = ', '.join(addresses)
            status = self._send_email(address_list, subject, html, attachment_ids=attachment_ids)
            feedback = {
                'queued': 'Notification mise en file d’envoi pour %s. Elle sera traitée par la tâche d’envoi des e-mails Odoo.',
            }.get(status, 'Échec de l’envoi à %s. Consultez les e-mails techniques pour le détail.')
            self._post_note(feedback % address_list)

    @api.depends('state', 'requester_employee_id.company_id', 'create_uid')
    @api.depends_context('uid')
    def _compute_can_manage(self):
        for record in self:
            record.can_manage = record._can_manage(self.env.user)

    def _can_manage(self, user):
        self.ensure_one()
        return (self.requester_employee_id.sudo().company_id.id in user._hr_request_management_company_ids()
                and not self._is_own_request(user))

    def _check_manager(self):
        """Combiner les droits Odoo et le périmètre métier, sans auto-validation."""
        self.ensure_one()
        self.check_access('write')
        if not self._can_manage(self.env.user):
            raise UserError(self.env._('Only a manager of this company can process this request, excluding their own requests.'))

    def _get_manager_users(self, exclude_own=True):
        """Trouver les gestionnaires actifs autorisés pour la société du demandeur."""
        self.ensure_one()
        groups = self.env.ref('hr_requests.group_hr_request_metraco') | self.env.ref('hr_requests.group_hr_request_daiko_damos')
        return self.env['res.users'].sudo().search([
            ('active', '=', True), ('share', '=', False), ('groups_id', 'in', groups.ids),
        ]).filtered(lambda user:
            self.requester_employee_id.sudo().company_id.id in user._hr_request_management_company_ids()
            and (not exclude_own or not self._is_own_request(user))
        )

    def action_submit(self):
        """Soumettre après contrôle du demandeur, du modèle de document et des gestionnaires."""
        self.ensure_one()
        self.check_access('write')
        if self.state != 'draft':
            raise UserError(self.env._('Only a draft request can be submitted.'))
        self._check_requester_access(self.requester_employee_id)
        if not self.request_type_id.active or self.request_type_id._document_code() not in (
                'work_certificate', 'salary_certificate', 'credit_request'):
            raise UserError(self.env._('No document template is configured for this request type.'))
        if not self._get_manager_users():
            company = self.requester_employee_id.sudo().company_id
            if self._get_manager_users(exclude_own=False):
                raise UserError(self.env._(
                    'No other manager can process this request for %(company)s. '
                    'A manager cannot process a request they created or benefit from. '
                    'Assign another active manager with access to this company.',
                    company=company.name,
                ))
            raise UserError(self.env._(
                'No active manager has access to %(company)s. Check the processing group '
                'and the allowed companies of the manager.', company=company.name,
            ))
        self._workflow_write({'state': 'submitted', 'submitted_date': fields.Datetime.now()})
        self._notify_current_validators()

    def _get_report_request(self):
        """Préparer les données du rapport dans la seule société autorisée.

        Vérifier le demandeur et le gestionnaire avant de changer le contexte.
        Aucun droit de lecture supplémentaire n'est accordé par sudo au rendu.
        """
        self.ensure_one()
        self._check_report_access()
        company = self.requester_employee_id.sudo().company_id
        if not company or company.id not in self.env.user.company_ids.ids:
            raise UserError(self.env._(
                'The requesting employee must belong to a company you are allowed to access.'
            ))
        report_request = self.with_context(
            allowed_company_ids=[company.id],
        ).with_company(company)
        try:
            employee = report_request.requester_employee_id
            employee.check_access('read')
            # Relational metadata is inspected only after request authorization.
            # Never read or expose the name of an inaccessible foreign department.
            for related in (report_request.department_id, employee.job_id):
                if related:
                    related_company = related.sudo().company_id
                    if related_company and related_company.id != company.id:
                        raise UserError(self.env._(
                            'The department or job on this request belongs to another company. '
                            'Ask HR to check the employee and request before generating the document.'
                        ))
                    related.check_access('read')
                    related.name
            employee.name
            company.with_env(report_request.env).read(['name', 'street', 'city', 'zip', 'email', 'phone', 'logo'])
        except AccessError as error:
            raise UserError(self.env._(
                'You cannot read the employee information required for this document. '
                'Ask HR to check the company, department, job and your access rights.'
            )) from error
        return report_request

    def action_download_to_sign(self):
        """Générer ou réutiliser le PDF non signé après vérification de son rattachement."""
        self.ensure_one()
        self._check_manager()
        if self.state != 'submitted':
            raise UserError(self.env._('Only a submitted request can be downloaded for signature.'))
        attachment = self.generated_document_id.sudo().exists()
        if not attachment or attachment.res_model != self._name or attachment.res_id != self.id:
            code = self.request_type_id._document_code()
            if code not in ('work_certificate', 'salary_certificate', 'credit_request'):
                raise UserError(self.env._('No document template is configured for this request type.'))
            report_request = self._get_report_request()
            report = report_request.env.ref('hr_requests.action_report_' + code)
            pdf, _ = report.with_context(lang='fr_FR')._render_qweb_pdf(
                report.report_name, res_ids=report_request.ids,
            )
            attachment = self.env['ir.attachment'].sudo().create({
                'name': '%s - %s.pdf' % (self._document_label_fr(), self.requester_employee_id.name),
                'datas': base64.b64encode(pdf), 'mimetype': 'application/pdf',
                'res_model': self._name, 'res_id': self.id,
            })
            self._workflow_write({'generated_document_id': attachment.id, 'generated_date': fields.Datetime.now()})
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % attachment.id, 'target': 'self'}

    def action_upload_signed(self):
        self.ensure_one()
        self._check_manager()
        if self.state != 'submitted':
            raise UserError(self.env._('Only a submitted request can receive a signed document.'))
        return {'type': 'ir.actions.act_window', 'name': self.env._('Upload Signed Document'),
                'res_model': 'hr.request.sign.wizard', 'view_mode': 'form', 'target': 'new',
                'context': {'default_request_id': self.id}}

    def action_reject(self):
        self.ensure_one()
        self._check_manager()
        if self.state != 'submitted':
            raise UserError(self.env._('Only a submitted request can be rejected.'))
        return {'type': 'ir.actions.act_window', 'name': self.env._('Reject Request'),
                'res_model': 'hr.request.reject.wizard', 'view_mode': 'form', 'target': 'new',
                'context': {'default_request_id': self.id}}

    def _reject_with_reason(self, reason):
        """Verrouiller la demande avant de vérifier son état et d’enregistrer le rejet."""
        self.ensure_one()
        self._check_manager()
        self.env.cr.execute('SELECT id FROM hr_request WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset(['state'])
        if self.state != 'submitted':
            raise UserError(self.env._('Only a submitted request can be rejected.'))
        reason = (reason or '').strip()
        if not reason:
            raise UserError(self.env._('Please enter a rejection reason.'))
        self._workflow_write({'state': 'rejected', 'rejection_reason': reason,
                              'rejected_by_id': self.env.uid, 'rejected_date': fields.Datetime.now()})
        self._notify_requester()

    def action_done(self):
        """Clôturer une demande signée sous verrou et notifier avec le document signé."""
        self.ensure_one()
        self._check_manager()
        self.env.cr.execute('SELECT id FROM hr_request WHERE id = %s FOR UPDATE', (self.id,))
        self.invalidate_recordset(['state', 'signed_document_id'])
        if self.state != 'signed' or not self._get_generated_attachment():
            raise UserError(self.env._('Upload the signed PDF before completing the request.'))
        self._workflow_write({'state': 'done'})
        self._notify_requester(attachment_ids=self.signed_document_id.ids)
        return True

    def _check_report_access(self):
        """Appliquer les contrôles du workflow aux appels directs du rapport QWeb."""
        self.ensure_one()
        self._check_manager()
        if self.state != 'submitted':
            raise UserError(self.env._('Only a submitted request can be downloaded for signature.'))
        return True

    def _get_generated_attachment(self):
        """Retourner uniquement un document rattaché à cette demande et accessible.

        Les demandes terminées historiques peuvent conserver leur ancien PDF généré ;
        ce repli ne signifie pas que ce document a été signé.
        """
        self.ensure_one()
        self.check_access('read')
        # Historical completed requests retain their old document, without claiming it is signed.
        attachment = (self.signed_document_id or (
            self.generated_document_id if self.state == 'done' else self.env['ir.attachment'])).sudo().exists()
        if (self.state not in ('signed', 'done') or not attachment
                or attachment.res_model != self._name or attachment.res_id != self.id):
            return self.env['ir.attachment']
        return attachment

    def action_download_document(self):
        self.ensure_one()
        attachment = self._get_generated_attachment()
        if not attachment:
            raise UserError(self.env._('No generated document is available.'))
        return {'type': 'ir.actions.act_url', 'url': '/web/content/%s?download=true' % attachment.id, 'target': 'self'}
