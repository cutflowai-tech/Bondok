"""Bondok runtime: the scheduled execution that n8n WF1/WF2/WF3 used to provide (agent migration P2/P4).

Every decision stays in ``waset_ops`` (the single command handler). This package only performs the external calls
(Monday, Dropbox, Instagram, the caption model) and the timing, through small clients with injectable transports so
that every path is testable without network access. Standard library only.
"""
