;;; org2docx.el --- Export Org documents to styled DOCX and PDF  -*- lexical-binding: t -*-

;; Copyright (c) 2026 Oleksii Koval

;; Author: Oleksii Koval
;; URL: https://github.com/USERNAME/org2docx
;; Version: 0.1.0
;; Package-Requires: ((emacs "27.1") (org "9.5"))
;; Keywords: outlines, wp, docx, pdf

;; This file is not part of GNU Emacs.
;; SPDX-License-Identifier: MIT

;;; Commentary:

;; org2docx exports the current Org buffer to a DOCX file and a PDF next to it, using the bundled
;; `org2docx.py' (pandoc + LibreOffice) and a style profile (`formal', `internal', or your own).
;;
;; Before calling the converter, the buffer is expanded with Org's own exporter (the `org' back-end):
;; #+INCLUDE, #+MACRO and #+BIND are resolved, :noexport: and COMMENT subtrees are removed, and the
;; result is written to a temporary file.  pandoc's Org reader does not implement these features, so
;; this step is what gives full Org support.  The converter runs asynchronously in the *org2docx*
;; buffer.
;;
;; Entry points:
;;   M-x org2docx-export           export the buffer (C-u: choose the profile)
;;   C-c C-e w w / w p / w o       the same from the export dispatcher (w o: open the PDF afterwards)
;;
;; Per-document settings live in the Org header:
;;   #+DOCX_PROFILE: internal   #+DOCX_TOC: nil   #+DOCX_PDF: t

;;; Code:

(require 'ox)
(require 'ox-org)
(require 'subr-x)

(defgroup org2docx nil
  "Export Org documents to styled DOCX and PDF."
  :group 'org-export
  :prefix "org2docx-")

(defcustom org2docx-directory
  (file-name-directory (or load-file-name buffer-file-name default-directory))
  "Directory that holds org2docx.py and the profiles/ directory.
Defaults to the directory this file was loaded from."
  :type 'directory)

(defcustom org2docx-script nil
  "Path of org2docx.py.  When nil, use org2docx.py in `org2docx-directory'."
  :type '(choice (const :tag "org2docx.py next to org2docx.el" nil) file))

(defcustom org2docx-python "python"
  "Python interpreter used to run org2docx.py."
  :type 'string)

(defcustom org2docx-default-profile nil
  "Profile used when the document has no #+DOCX_PROFILE keyword.
When nil, the converter's own default (\"formal\") applies."
  :type '(choice (const :tag "Converter default" nil) string))

(defcustom org2docx-open-pdf 'ask
  "What to do with the PDF after a successful export.
`ask' asks whether to open it, `always' opens it, `never' only
reports the paths."
  :type '(choice (const ask) (const always) (const never)))

(defcustom org2docx-use-babel nil
  "Non-nil means evaluate Babel source blocks during the pre-expansion.
The default nil keeps every source block as written (no code runs
during export); existing #+RESULTS blocks are exported as they are."
  :type 'boolean)

(defcustom org2docx-extra-args nil
  "Extra command-line arguments for org2docx.py.
For example (\"--keep-intermediate\")."
  :type '(repeat string))

(defvar org2docx-log-buffer "*org2docx*"
  "Name of the buffer that receives the converter output.")

(defun org2docx--script ()
  "Return the absolute path of org2docx.py."
  (expand-file-name (or org2docx-script (expand-file-name "org2docx.py" org2docx-directory))))

(defun org2docx-profiles ()
  "Return the names of the available profiles."
  (let ((dir (expand-file-name "profiles" (file-name-directory (org2docx--script)))))
    (when (file-directory-p dir)
      (seq-filter (lambda (name)
                    (file-exists-p (expand-file-name "profile.json" (expand-file-name name dir))))
                  (directory-files dir nil "\\`[^.]")))))

(defun org2docx--read-profile ()
  "Ask for a profile name."
  (completing-read "org2docx profile: " (org2docx-profiles) nil t nil nil
                   (or org2docx-default-profile "formal")))

(defun org2docx-expand-buffer (&optional subtreep visible-only)
  "Expand the current Org buffer with Org's exporter; return a string.
Includes, macros and #+BIND are resolved; :noexport: and COMMENT
subtrees are dropped.  SUBTREEP and VISIBLE-ONLY are as in
`org-export-as'.  A #+TITLE, #+AUTHOR or #+DATE line is written only
when the buffer has that keyword, so no file-name title or default
author appears.  Property drawers are kept so that CUSTOM_ID link
targets survive."
  (let* ((kw (org-collect-keywords '("TITLE" "AUTHOR" "DATE")))
         (ext (append (unless (assoc "TITLE" kw) '(:with-title nil))
                      (unless (assoc "AUTHOR" kw) '(:with-author nil))
                      (unless (assoc "DATE" kw) '(:with-date nil))
                      '(:with-properties t :with-email nil :with-creator nil
                        :time-stamp-file nil)))
         (org-export-use-babel org2docx-use-babel))
    (org-export-as 'org2docx subtreep visible-only nil ext)))

(defun org2docx--sentinel (proc _event)
  "Report the result of the converter process PROC."
  (when (memq (process-status proc) '(exit signal))
    (let* ((tmp (process-get proc :org2docx-tmp))
           (docx (process-get proc :org2docx-docx))
           (pdf (process-get proc :org2docx-pdf))
           (open (process-get proc :org2docx-open))
           (status (process-exit-status proc))
           (pdf-made (and pdf (file-exists-p pdf)
                          (time-less-p (process-get proc :org2docx-start)
                                       (file-attribute-modification-time (file-attributes pdf))))))
      (when (and tmp (file-exists-p tmp))
        (delete-file tmp))
      (process-put proc :org2docx-done t)
      (if (and (eq (process-status proc) 'exit) (zerop status))
          (progn
            (message "org2docx: wrote %s%s" docx (if pdf-made (format " and %s" pdf) ""))
            (when (and pdf-made (not noninteractive)
                       (pcase open
                         ('always t)
                         ('ask (y-or-n-p (format "Open %s? " (file-name-nondirectory pdf))))
                         (_ nil)))
              (org-open-file pdf)))
        (message "org2docx: export failed (exit status %s); see %s" status org2docx-log-buffer)
        (unless noninteractive
          (display-buffer (process-buffer proc)))))))

(defun org2docx-export-file (&optional profile subtreep visible-only open)
  "Export the current Org buffer to DOCX/PDF; return the process.
PROFILE overrides #+DOCX_PROFILE.  SUBTREEP and VISIBLE-ONLY restrict
the export as in `org-export-as'.  OPEN overrides `org2docx-open-pdf'.
Output goes next to the Org file as <basename>.docx and .pdf."
  (unless (derived-mode-p 'org-mode)
    (user-error "Not an Org buffer"))
  (unless buffer-file-name
    (user-error "Save the buffer to a file first: output goes next to it"))
  (let ((script (org2docx--script)))
    (unless (file-exists-p script)
      (user-error "org2docx.py not found at %s (customize `org2docx-directory')" script))
    (unless (executable-find org2docx-python)
      (user-error "Python interpreter `%s' not found" org2docx-python))
    (when (buffer-modified-p) (save-buffer))
    (let* ((src buffer-file-name)
           (dir (file-name-directory src))
           (base (file-name-base src))
           (expanded (org2docx-expand-buffer subtreep visible-only))
           (tmp (make-temp-file (concat "org2docx-" base "-") nil ".org"))
           (args (append (list (org2docx--script) tmp "-o" dir "--name" base "--resource-path" dir)
                         (when profile (list "--profile" profile))
                         (when (and (not profile) org2docx-default-profile)
                           (list "--default-profile" org2docx-default-profile))
                         org2docx-extra-args))
           (buf (get-buffer-create org2docx-log-buffer)))
      (let ((coding-system-for-write 'utf-8))
        (with-temp-file tmp (insert expanded)))
      (with-current-buffer buf
        (let ((inhibit-read-only t))
          (erase-buffer)
          (insert (format "$ %s %s\n\n" org2docx-python (mapconcat #'shell-quote-argument args " ")))))
      (let ((proc (make-process :name "org2docx"
                                :buffer buf
                                :command (cons org2docx-python args)
                                :connection-type 'pipe
                                :coding 'utf-8
                                :sentinel #'org2docx--sentinel)))
        (process-put proc :org2docx-tmp tmp)
        (process-put proc :org2docx-docx (expand-file-name (concat base ".docx") dir))
        (process-put proc :org2docx-pdf (expand-file-name (concat base ".pdf") dir))
        (process-put proc :org2docx-open (or open org2docx-open-pdf))
        (process-put proc :org2docx-start (current-time))
        (message "org2docx: exporting %s ..." (file-name-nondirectory src))
        proc))))

;;;###autoload
(defun org2docx-export (&optional ask-profile)
  "Export the current Org buffer to <basename>.docx and .pdf beside it.
With a prefix argument ASK-PROFILE, ask for the profile (otherwise
#+DOCX_PROFILE, `org2docx-default-profile' or the converter default
applies)."
  (interactive "P")
  (org2docx-export-file (when ask-profile (org2docx--read-profile))))

;;;###autoload
(defalias 'my/org2docx-export #'org2docx-export)

(defun org2docx--dispatch (_async subtreep visible-only _body-only)
  "Export dispatcher action: default profile."
  (org2docx-export-file nil subtreep visible-only))

(defun org2docx--dispatch-profile (_async subtreep visible-only _body-only)
  "Export dispatcher action: ask for the profile."
  (org2docx-export-file (org2docx--read-profile) subtreep visible-only))

(defun org2docx--dispatch-open (_async subtreep visible-only _body-only)
  "Export dispatcher action: export and open the PDF."
  (org2docx-export-file nil subtreep visible-only 'always))

(defun org2docx--export-block (export-block _contents _info)
  "Keep EXPORT-BLOCK of any type (e.g. openxml) for pandoc.
The plain `org' back-end keeps only #+begin_export org blocks."
  (org-element-export-block-interpreter export-block nil))

(defun org2docx--export-snippet (export-snippet _contents _info)
  "Keep EXPORT-SNIPPET of any back-end (e.g. @@openxml:...@@) for pandoc."
  (org-element-export-snippet-interpreter export-snippet nil))

(org-export-define-derived-backend 'org2docx 'org
  :translate-alist '((export-block . org2docx--export-block)
                     (export-snippet . org2docx--export-snippet))
  :menu-entry
  '(?w "Export to DOCX/PDF (org2docx)"
       ((?w "As DOCX/PDF" org2docx--dispatch)
        (?p "As DOCX/PDF, choose profile" org2docx--dispatch-profile)
        (?o "As DOCX/PDF and open PDF" org2docx--dispatch-open))))

(provide 'org2docx)

;;; org2docx.el ends here
