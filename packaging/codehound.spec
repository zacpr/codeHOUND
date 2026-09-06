Name:           codehound
Version:        0.2.0
Release:        3%{?dist}
Summary:        Repo hunter and launcher dashboard

License:        MIT
URL:            https://github.com/zacpr/codeHOUND
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3-wheel
Requires:       python3 >= 3.11
Requires:       python3-pyside6
Requires:       git

%description
Hunts down git repos and stray software projects across your disks;
tray dashboard to find, launch, and organise them. Syncs GitHub and
Azure DevOps accounts, asks AI about your catalogue, and cleans up
finished work.

%prep
%autosetup

%build
# nothing to compile — pure Python

%install
# install the package into site-packages
python3 -m pip install --root %{buildroot} --no-deps --no-build-isolation .

# wrapper script
mkdir -p %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/codehound << 'EOF'
#!/usr/bin/python3
from codehound.cli import main
import sys
sys.exit(main())
EOF
chmod 755 %{buildroot}%{_bindir}/codehound

# desktop file
mkdir -p %{buildroot}%{_datadir}/applications
install -m 644 codehound/assets/codehound.desktop %{buildroot}%{_datadir}/applications/

# icon
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
install -m 644 codehound/assets/hound.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/codehound.svg

# remove pip's entry point (we have our own wrapper)
rm -f %{buildroot}%{_bindir}/codehound  # pip may have written one; overwrite below
cat > %{buildroot}%{_bindir}/codehound << 'EOF'
#!/usr/bin/python3
from codehound.cli import main
import sys
sys.exit(main())
EOF
chmod 755 %{buildroot}%{_bindir}/codehound

%files
%{_bindir}/codehound
%{python3_sitelib}/codehound/
%{python3_sitelib}/codehound-*.dist-info/
%{_datadir}/applications/codehound.desktop
%{_datadir}/icons/hicolor/scalable/apps/codehound.svg

%post
# refresh desktop database and icon cache
/usr/bin/update-desktop-database %{_datadir}/applications &> /dev/null || :
/usr/bin/gtk-update-icon-cache %{_datadir}/icons/hicolor &> /dev/null || :

%postun
/usr/bin/update-desktop-database %{_datadir}/applications &> /dev/null || :
/usr/bin/gtk-update-icon-cache %{_datadir}/icons/hicolor &> /dev/null || :

%changelog
* Sun Sep 06 2026 zacpr <zac@ashurtech.net> - 0.2.0-3
- Fix settings/janitor dialogs leaking ~0.7 MB each time they were opened
  (Qt object cycles kept the whole dialog alive; now WA_DeleteOnClose)
- Ask AI: when no provider is configured, clicking advises the user and
  offers to open Settings → AI; tray/context entries are disabled with a
  hint until then

* Sun Sep 06 2026 zacpr <zac@ashurtech.net> - 0.2.0-2
- Desktop entry: StartupNotify + StartupWMClass so menu launches give
  feedback and the window groups with its hound icon
- install-autostart: refresh desktop/icon caches so the menu icon
  appears straight away; keep autostart entry quiet at login

* Sun Sep 06 2026 zacpr <zac@ashurtech.net> - 0.2.0-1
- Settings screen, account mirroring, Ask AI, janitor, UX polish
- Initial RPM packaging
