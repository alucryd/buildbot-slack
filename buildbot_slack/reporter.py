# Based on the gitlab reporter from buildbot

from twisted.internet import defer
from twisted.python import log

from buildbot.interfaces import IRenderable
from buildbot.process.results import statusToString
from buildbot.reporters.base import ReporterBase
from buildbot.reporters.generators.build import BuildStatusGenerator
from buildbot.reporters.message import MessageFormatterBase
from buildbot.util import httpclientservice

STATUS_EMOJIS = {
    "success": ":sunglassses:",
    "warnings": ":meow_wow:",
    "failure": ":skull:",
    "skipped": ":slam:",
    "exception": ":skull:",
    "retry": ":facepalm:",
    "cancelled": ":slam:",
}
STATUS_COLORS = {
    "success": "#36a64f",
    "warnings": "#fc8c03",
    "failure": "#fc0303",
    "skipped": "#fc8c03",
    "exception": "#fc0303",
    "retry": "#fc8c03",
    "cancelled": "#fc8c03",
}
DEFAULT_HOST = "https://hooks.slack.com"  # deprecated


class SlackMessageFormatter(MessageFormatterBase):
    """Builds the Slack webhook payload for a single build.

    The returned ``body`` is a JSON-serializable dict that is posted verbatim
    to the Slack incoming webhook by :class:`SlackStatusPush`.
    """

    template_type = "json"

    def __init__(self, channel=None, username=None, attachments=True, verbose=False):
        super().__init__(want_properties=True)
        self.channel = channel
        self.username = username
        self.attachments = attachments
        self.verbose = verbose

    def format_message_for_build(
        self, master, build, is_buildset=False, users=None, mode=None
    ):
        post_data = {"text": self._get_message(build)}

        if self.attachments:
            attachments = self._get_attachments(build)
            if attachments:
                post_data["attachments"] = attachments
        else:
            post_data["text"] += " here: " + build.get("url", "")

        if self.channel:
            post_data["channel"] = self.channel

        if self.username:
            post_data["username"] = self.username

        post_data["icon_emoji"] = STATUS_EMOJIS.get(
            statusToString(build["results"]), ":facepalm:"
        )

        return {
            "body": post_data,
            "type": self.template_type,
            "subject": None,
            "extra_info": None,
        }

    def _get_message(self, build):
        if not build["complete"]:
            return "Buildbot started build %s" % build["builder"]["name"]
        return "Buildbot finished build %s with result: %s" % (
            build["builder"]["name"],
            statusToString(build["results"]),
        )

    def _get_attachments(self, build):
        sourcestamps = build["buildset"]["sourcestamps"]
        attachments = []

        for sourcestamp in sourcestamps:
            sha = sourcestamp["revision"]

            title = "Build #{buildid}".format(buildid=build["buildid"])
            project = sourcestamp["project"]
            if project:
                title += " for {project} {sha}".format(project=project, sha=sha)
            sub_build = bool(build["buildset"]["parent_buildid"])
            if sub_build:
                title += " {relationship}: #{parent_build_id}".format(
                    relationship=build["buildset"]["parent_relationship"],
                    parent_build_id=build["buildset"]["parent_buildid"],
                )

            fields = []
            if not sub_build:
                branch_name = sourcestamp["branch"]
                if branch_name:
                    fields.append(
                        {"title": "Branch", "value": branch_name, "short": True}
                    )
                repositories = sourcestamp["repository"]
                if repositories:
                    fields.append(
                        {"title": "Repository", "value": repositories, "short": True}
                    )

            attachments.append(
                {
                    "title": title,
                    "title_link": build["url"],
                    "fallback": "{}: <{}>".format(title, build["url"]),
                    "text": "Status: *{status}*".format(
                        status=statusToString(build["results"])
                    ),
                    "color": STATUS_COLORS.get(statusToString(build["results"]), ""),
                    "mrkdwn_in": ["text", "title", "fallback"],
                    "fields": fields,
                }
            )
        return attachments


class SlackStatusPush(ReporterBase):
    """Buildbot reporter that sends build status updates to Slack."""

    name = "SlackStatusPush"

    def checkConfig(
        self,
        endpoint,
        channel=None,
        host_url=None,  # deprecated
        username=None,
        attachments=True,
        verbose=False,
        debug=None,
        verify=None,
        generators=None,
        **kwargs,
    ):
        # endpoint/channel/username may be Buildbot renderables (e.g. a Secret),
        # which are only resolved at reconfig time, so skip type checks for those.
        if not IRenderable.providedBy(endpoint):
            if not isinstance(endpoint, str):
                log.err(
                    "[SlackStatusPush] endpoint should be a string, got '%s' instead"
                    % type(endpoint).__name__
                )
            elif not endpoint.startswith("http"):
                log.err(
                    '[SlackStatusPush] endpoint should start with "http...", endpoint: %s'
                    % endpoint
                )
        if channel and not IRenderable.providedBy(channel) and not isinstance(channel, str):
            log.err(
                "[SlackStatusPush] channel must be a string, got '%s' instead"
                % type(channel).__name__
            )
        if username and not IRenderable.providedBy(username) and not isinstance(username, str):
            log.err(
                "[SlackStatusPush] username must be a string, got '%s' instead"
                % type(username).__name__
            )
        if host_url:
            log.msg(
                "[SlackStatusPush] argument host_url is deprecated and will be "
                "removed in the next release: specify the full url as endpoint"
            )

        if generators is None:
            generators = self._create_generators(
                channel, username, attachments, verbose
            )

        super().checkConfig(generators=generators, **kwargs)

    @defer.inlineCallbacks
    def reconfigService(
        self,
        endpoint,
        channel=None,
        host_url=None,  # deprecated
        username=None,
        attachments=True,
        verbose=False,
        debug=None,
        verify=None,
        generators=None,
        **kwargs,
    ):
        self.debug = debug
        self.verify = verify

        if generators is None:
            generators = self._create_generators(
                channel, username, attachments, verbose
            )

        # endpoint (and the deprecated host_url) may be renderables, e.g. a
        # Secret holding the webhook URL. Render before creating the session.
        server_url = yield self.renderSecrets(host_url or endpoint)

        yield super().reconfigService(generators=generators, **kwargs)

        self._http = yield httpclientservice.HTTPSession(
            self.master.httpservice,
            server_url,
            debug=self.debug,
            verify=self.verify,
        )

    def _create_generators(self, channel, username, attachments, verbose):
        formatter = SlackMessageFormatter(
            channel=channel,
            username=username,
            attachments=attachments,
            verbose=verbose,
        )
        return [
            BuildStatusGenerator(
                mode="all", message_formatter=formatter, report_new=True
            )
        ]

    def is_status_2xx(self, code):
        return code // 100 == 2

    @defer.inlineCallbacks
    def sendMessage(self, reports):
        response = yield self._http.post("", json=reports[0]["body"])
        if not self.is_status_2xx(response.code):
            content = yield response.content()
            log.err(
                "[SlackStatusPush] %s: unable to upload status: %s"
                % (response.code, content)
            )
