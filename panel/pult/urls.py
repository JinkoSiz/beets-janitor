from django.urls import path

from . import auth, views

urlpatterns = [
    path("login", auth.login_view, name="login"),
    path("logout", auth.logout_view, name="logout"),
    path("health", auth.health, name="health"),

    path("", views.summary, name="summary"),
    path("run", views.run_now, name="run"),

    path("review/", views.review, name="review"),
    path("review/<int:rid>/decide", views.review_decide, name="review_decide"),
    path("review/<int:rid>/undo", views.review_undo, name="review_undo"),
    path("review/<int:rid>/replace", views.review_replace, name="review_replace"),

    path("releases/", views.releases, name="releases"),
    path("releases/get-all", views.release_get_all, name="release_get_all"),
    path("releases/<int:rel_id>/get", views.release_get, name="release_get"),
    path("releases/<int:rel_id>/skip", views.release_skip, name="release_skip"),
    path("flag", views.set_flag, name="flag"),

    path("artists/", views.artists, name="artists"),
    path("artists/search", views.artists_search, name="artists_search"),
    path("artists/add", views.artists_add, name="artists_add"),
    path("artists/<int:aid>/follow", views.artist_follow, name="artist_follow"),
    path("artists/<int:aid>/pick", views.artist_pick, name="artist_pick"),

    path("discography/", views.discography, name="discography"),
    path("discography/start", views.disco_start, name="disco_start"),
    path("discography/job/<int:job_id>", views.disco_job, name="disco_job"),
    path("discography/<int:aid>/download", views.disco_download, name="disco_download"),
    path("discography/track/<int:tid>", views.disco_track, name="disco_track"),

    path("covers/", views.covers, name="covers"),
    path("covers/<int:rid>/upload", views.cover_upload, name="cover_upload"),

    path("quarantine/", views.quarantine, name="quarantine"),
    path("quarantine/<int:qid>/restore", views.q_restore, name="q_restore"),

    path("journal/", views.journal, name="journal"),
    path("journal/<int:eid>/rollback", views.j_rollback, name="j_rollback"),

    path("settings/", views.settings_view, name="settings"),
    path("settings/set", views.settings_set, name="settings_set"),

    path("audio/item/<int:iid>", views.audio_item, name="audio_item"),
    path("audio/q/<int:qid>", views.audio_quarantine, name="audio_q"),
    path("audio/ref/<int:rid>", views.audio_ref, name="audio_ref"),
    path("audio/deezer/<int:track_id>", views.audio_deezer, name="audio_deezer"),
]
