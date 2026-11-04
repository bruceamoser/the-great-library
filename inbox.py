"""Inbox inventory and lossless, staged imports. No archive paths are trusted."""
import json, os, shutil, stat, subprocess, tempfile, time, uuid, zipfile
from pathlib import Path, PurePosixPath
import jobs

MAX_UPLOAD = 2 * 1024**3
MAX_EXPANDED = 20 * 1024**3
MAX_MEMBERS = 20000

def component(value):
    if not isinstance(value,str) or not value.strip() or value != value.strip() or value in ('.','..') or any(c in value for c in '/\\\x00') or any(ord(c)<32 for c in value) or value.startswith('.') or len(value)>200:
        raise ValueError('Use a plain name without slashes, hidden prefixes, or control characters.')
    return value

def inventory(library):
    root=Path(library)/'_Inbox'
    if not root.exists():return []
    entries=[]
    for path in sorted(root.iterdir(),key=lambda p:p.name.casefold()):
        if path.name.startswith('.') or path.name.lower()=='readme.md':continue
        files=list(path.rglob('*')) if path.is_dir() and not path.is_symlink() else [path]
        blocked=path.is_symlink() or any(p.is_symlink() for p in files)
        regular=[p for p in files if p.is_file() and not p.is_symlink()]
        entries.append(dict(name=path.name,files=len(regular),size=sum(p.stat().st_size for p in regular),kind='Folder' if path.is_dir() else ('ZIP archive' if path.suffix.lower()=='.zip' else 'File'),blocked=blocked))
    return entries

def source_path(library,name):
    root=Path(library)/'_Inbox';path=root/component(name)
    if path.is_symlink() or not path.exists():raise ValueError('Inbox item is missing or is a symbolic link: '+name)
    if path.is_dir() and any(p.is_symlink() for p in path.rglob('*')):raise ValueError('Folders containing symbolic links cannot be imported.')
    return path

def extract(source,destination):
    with zipfile.ZipFile(source) as archive:
        members=archive.infolist()
        if len(members)>MAX_MEMBERS or sum(m.file_size for m in members)>MAX_EXPANDED:raise ValueError('ZIP exceeds the extraction limit (20 GB or 20,000 entries).')
        seen=set()
        for m in members:
            name=m.filename.replace('\\','/');p=PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts or not p.parts or ':' in p.parts[0] or stat.S_ISLNK(m.external_attr>>16):raise ValueError('Unsafe ZIP entry: '+m.filename)
            if any(part.startswith('.') for part in p.parts) or '__MACOSX' in p.parts:continue
            if name in seen:raise ValueError('Duplicate ZIP entry: '+name)
            seen.add(name)
            target=destination.joinpath(*p.parts)
            if m.is_dir():target.mkdir(parents=True,exist_ok=True);continue
            target.parent.mkdir(parents=True,exist_ok=True)
            with archive.open(m) as incoming,target.open('xb') as outgoing:shutil.copyfileobj(incoming,outgoing,1024*1024)

def process(library,names,title,shelf,command,fd):
    """Publish a new collection, archive originals, then refresh the catalog."""
    title=component(title);component(shelf)
    sources=[source_path(library,name) for name in names]
    destination=Path(library)/'Books'/shelf/title
    if destination.exists():raise ValueError('That collection already exists. Choose a new collection name.')
    batch=uuid.uuid4().hex
    archive=jobs.BASE/'inbox-originals'/batch
    # Stage outside the scanned library so partial collections are never indexed.
    with tempfile.TemporaryDirectory(prefix='inbox-import-',dir=jobs.BASE) as staging:
        staged=Path(staging)/title;staged.mkdir()
        for index,source in enumerate(sources):
            jobs.record('inbox',message='Preparing '+source.name,completed=index,total=len(sources))
            if source.is_dir():shutil.copytree(source,staged/source.name)
            elif source.suffix.lower()=='.zip':extract(source,staged/source.stem)
            else:shutil.copy2(source,staged/source.name)
        if not any(p.is_file() for p in staged.rglob('*')):raise ValueError('No files were found to import.')
        destination.parent.mkdir(parents=True,exist_ok=True)
        # mkdir claims the name without replacing another collection.
        destination.mkdir()
        try:shutil.copytree(staged,destination,dirs_exist_ok=True)
        except Exception:
            shutil.rmtree(destination)
            raise
        archived=[]
        try:
            archive.mkdir(parents=True)
            (archive/'import-manifest.json').write_text(json.dumps({'destination':str(destination),'sources':[str(p) for p in sources],'created':time.time()},indent=2))
            for source in sources:
                shutil.move(str(source),str(archive/source.name));archived.append(source.name)
        except Exception:
            for name in archived:shutil.move(str(archive/name),str(Path(library)/'_Inbox'/name))
            shutil.rmtree(destination)
            raise
    jobs.record('inbox',message='Updating the catalog and classifications…',completed=len(sources),destination=str(destination.relative_to(library)),originals=str(archive))
    if command is None:return destination
    result=subprocess.run(command,capture_output=True,text=True,**jobs.child_options(fd))
    if result.returncode:raise RuntimeError('Files were imported and originals retained, but catalog refresh failed. Use Rebuild catalog in the Keeper’s Office. '+result.stderr[-1000:])
    return destination

def plan(library,con):
    """Conservative batch routing from known games, shelf rules, and archive names."""
    import re, collections
    import classification, taxonomy
    from catalog import META
    known={}
    for alias,system,edition in classification.SYSTEMS:known[classification.norm(alias)]=(system,edition)
    for name in META:known.setdefault(classification.norm(name),(classification.words(name),''))
    shelves=collections.defaultdict(collections.Counter)
    try:
        for row in con.execute("SELECT b.system,b.edition,c.shelf,COUNT(*) n FROM book_classification b JOIN files f USING(rel_path) JOIN collections c ON c.id=f.collection_id WHERE b.system!='Unassigned system' AND c.shelf NOT LIKE '\\_%' ESCAPE '\\' GROUP BY b.system,b.edition,c.shelf"):
            known.setdefault(classification.norm(row['system']),(row['system'],row['edition']))
            shelves[(row['system'],row['edition'])][row['shelf']]+=row['n']
    except Exception:pass
    for row in con.execute("SELECT name,shelf FROM collections WHERE shelf NOT IN ('_Inbox','_Unsorted')"):
        known.setdefault(classification.norm(row['name']),(row['name'],''))
        shelves[(row['name'],'')][row['shelf']]+=1
    patterns=[(alias,value,re.compile(r'\b'+re.escape(alias)+r'\b')) for alias,value in known.items() if len(alias)>2]
    def identify(text):
        text=classification.norm(text)
        matches=[(alias,value) for alias,value,pattern in patterns if pattern.search(text)]
        return max(matches,key=lambda match:len(match[0]))[1] if matches else None
    def loose_title(name):
        cleaned=classification.words(Path(name).stem)
        title=re.split(r'\s*[-–]\s*|\b(?:rpg|core|rulebook|maps?|playbooks?|starter|corrected|\d)\b',cleaned,flags=re.I)[0].strip()
        return title or cleaned
    arrivals=inventory(library)
    acronym_titles=collections.defaultdict(set)
    for entry in arrivals:
        title=loose_title(entry['name']);parts=title.split()
        if len(parts)>=3:
            acronym=''.join(part[0] for part in parts).upper()
            if len(acronym)>=3:acronym_titles[acronym].add(title)
    grouped={};review=[]
    for entry in arrivals:
        name=entry['name'];source=source_path(library,name) if not entry['blocked'] else None
        reason=None;game=None
        if source is None:reason='Contains a symbolic link.'
        else:
            game=identify(name)
            if not game and source.suffix.lower()=='.zip':
                try:
                    with zipfile.ZipFile(source) as z:
                        if len(z.infolist())>MAX_MEMBERS:raise ValueError('Archive has too many entries.')
                        candidates={found for item in z.namelist() if (found:=identify(item))}
                        if len(candidates)==1:game=candidates.pop()
                        elif len(candidates)>1:reason='Archive contains several games; choose a collection after review.'
                except (zipfile.BadZipFile,ValueError) as exc:reason=str(exc)
            if not game and not reason:
                # A recognizable title rule can classify a new game not yet in the catalog.
                title=loose_title(name)
                shelf,_,rule=taxonomy.classify_title(title,'book','_Unsorted',(None,None,None,[]))
                if shelf!='_Unsorted':game=(title,'')
            if not game and not reason:
                title=loose_title(name)
                expanded=acronym_titles.get(title.upper(),set())
                game=(next(iter(expanded)) if len(expanded)==1 else title,'')
        if reason:review.append({**entry,'reason':reason});continue
        system,edition=game
        shelf,_,rule=taxonomy.classify_title(system,'book','_Unsorted',(None,None,None,[]))
        if shelf=='_Unsorted':
            counts=collections.Counter()
            for (candidate,ed),votes in shelves.items():
                if candidate==system:counts.update(votes)
            if counts and len(counts)==1:shelf=next(iter(counts))
        uncertain=shelf=='_Unsorted'
        title=system+(' '+edition if edition else '')
        # Existing catalog entries remain untouched; new arrivals are grouped together.
        title=re.sub(r'[/\\\x00]',' - ',title).strip()[:180]
        key=(shelf,title)
        g=grouped.setdefault(key,dict(title=title,shelf=shelf,items=[],signatures=[],size=0,needs_review=uncertain,reason=('Uncertain game or shelf; safely placed in Unsorted for later review' if uncertain else 'Recognized '+system+(' ('+edition+')' if edition else '')+'; shelf from '+('title rules' if rule!='folder-only' else 'existing catalog'))))
        g['items'].append(name);g['size']+=entry['size']
        g['signatures'].append([name,str(source.stat().st_mtime_ns),entry['size']])
    for group in grouped.values():
        root=Path(library)/'Books'/group['shelf'];base=group['title'];number=1
        while (root/group['title']).exists():
            group['title']=base+' — Arrivals'+(' '+str(number) if number>1 else '');number+=1
        group['destination']='Books/'+group['shelf']+'/'+group['title']
    return {'groups':list(grouped.values()),'review':review,'count':sum(len(g['items']) for g in grouped.values())}

def process_batch(library,groups,command,fd):
    results=[];errors=[]
    for index,group in enumerate(groups):
        jobs.record('inbox',message='Importing collection '+str(index+1)+' of '+str(len(groups))+': '+group['title'],batch_completed=index,batch_total=len(groups))
        try:
            destination=process(library,group['items'],group['title'],group['shelf'],None,fd)
            results.append(str(destination.relative_to(library)))
        except Exception as exc:errors.append(group['title']+': '+str(exc))
        jobs.record('inbox',results=results,errors=errors,batch_completed=index+1)
    if results:
        jobs.record('inbox',message='Updating the catalog and classifications…')
        result=subprocess.run(command,capture_output=True,text=True,**jobs.child_options(fd))
        if result.returncode:errors.append('Catalog refresh failed. Files and originals are retained. Rebuild the catalog in the Keeper’s Office. '+result.stderr[-1000:])
    return results,errors
