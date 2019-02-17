extern crate structopt;
#[macro_use]
extern crate log;
extern crate flate2;
extern crate indicatif;
extern crate bookdata;
extern crate zip;
extern crate postgres;
extern crate ntriple;
extern crate snap;

use std::io::prelude::*;
use std::io::{BufReader, BufWriter};
use std::collections::HashMap;

use structopt::StructOpt;
use std::fs;
use std::path::{Path, PathBuf};
use zip::read::ZipArchive;
use indicatif::{ProgressBar, ProgressStyle};
use postgres::Connection;

use ntriple::parser::triple_line;
use ntriple::{Subject, Predicate, Object};

use bookdata::cleaning::{write_pgencoded};
use bookdata::{log_init, Result};

/// Import n-triples RDF (e.g. from LOC) into a database.
#[derive(StructOpt, Debug)]
#[structopt(name="import-ntriples")]
struct Opt {
  /// Verbose mode (-v, -vv, -vvv, etc.)
  #[structopt(short="v", long="verbose", parse(from_occurrences))]
  verbose: usize,
  /// Silence output
  #[structopt(short="q", long="quiet")]
  quiet: bool,
  /// Database URL to connect to
  #[structopt(long="db-url")]
  db_url: Option<String>,
  /// Database schema
  #[structopt(long="db-schema")]
  db_schema: Option<String>,
  /// Input file
  #[structopt(name = "INPUT", parse(from_os_str))]
  infile: PathBuf,
  /// Output directory
  #[structopt(name = "OUTPUT", parse(from_os_str))]
  outdir: PathBuf
}

struct NodeIndex<W: Write> {
  table: HashMap<String,i64>,
  max: i64,
  file: W,
  name: String
}

impl<W: Write> NodeIndex<W> {
  fn create(out: W, name: &str) -> NodeIndex<W> {
    NodeIndex {
      table: HashMap::new(),
      max: 0,
      file: out,
      name: name.to_string()
    }
  }

  fn load(&mut self, db: &Connection, opt: &Opt) -> Result<()> {
    let tbl = match &(opt.db_schema) {
      Some(s) => format!("{}.nodes", s),
      None => "nodes".to_string()
    };
    let max_iri_query = format!("SELECT COALESCE(MAX(node_id), 0) FROM {}", tbl);
    for row in &db.query(&max_iri_query, &[])? {
      self.max = row.get(0);
    }
    info!("database has max node ID {}", self.max);

    let query = format!("SELECT node_id, node_iri FROM {} WHERE node_iri NOT LIKE 'blank://%'", tbl);
    
    for row in &db.query(&query, &[])? {
      let id: i64 = row.get(0);
      let iri: String = row.get(1);
      self.table.insert(iri, id);
    }
    Ok(())
  }

  fn node_id(&mut self, iri: &str) -> Result<i64> {
    let id = self.table.entry(iri.to_string()).or_insert(self.max + 1);
    let id = *id;
    if id > self.max {
      self.max = id;
      write!(&mut self.file, "{}\t{}\n", id, iri)?;
    }
    Ok(id)
  }

  fn blank_id(&mut self, key: &str) -> Result<i64> {
    let iri = format!("blank://{}/{}", self.name, key);
    self.node_id(&iri)
  }
  
  fn subj_id(&mut self, sub: &Subject) -> Result<i64> {
    match sub {
      Subject::IriRef(iri) => self.node_id(iri),
      Subject::BNode(key) => self.blank_id(key)
    }
  }

  fn pred_id(&mut self, pred: &Predicate) -> Result<i64> {
    match pred {
      Predicate::IriRef(iri) => self.node_id(iri)
    }
  }
}

struct LitWriter<W: Write> {
  file: W,
  last: i64
}

impl<W: Write> LitWriter<W> {
  fn create(out: W) -> LitWriter<W> {
    LitWriter {
      file: out, last: 0
    }
  }

  fn load(&mut self, db: &Connection, opt: &Opt) -> Result<()> {
    let tbl = match &(opt.db_schema) {
      Some(s) => format!("{}.literals", s),
      None => "literals".to_string()
    };
    let min_lit_query = format!("SELECT COALESCE(MIN(lit_id), 0) FROM {}", tbl);
    for row in &db.query(&min_lit_query, &[])? {
      let min: i64 = row.get(0);
      self.last = -min;
    }
    info!("database has min literal ID {}", -self.last);
    Ok(())
  }

  fn lit_id(&mut self, lit: &str) -> Result<i64> {
    let id = self.last + 1;
    self.last += 1;
    write!(&mut self.file, "{}\t", -id)?;
    write_pgencoded(&mut self.file, lit.as_bytes())?;
    self.file.write_all(b"\n")?;
    Ok(-id)
  }
}

fn obj_id<W: Write>(nodes: &mut NodeIndex<W>, lits: &mut LitWriter<W>, obj: &Object) -> Result<i64> {
  match obj {
    Object::IriRef(iri) => nodes.node_id(iri),
    Object::BNode(key) => nodes.blank_id(key),
    Object::Lit(l) => lits.lit_id(&l.data)
  }
} 

fn open_out(dir: &Path, name: &str) -> Result<Box<Write>> {
  let mut buf = dir.to_path_buf();
  buf.push(name);
  let file = fs::OpenOptions::new().write(true).create(true).open(buf)?;
  let file = snap::Writer::new(file);
  let file = BufWriter::new(file);
  Ok(Box::new(file))
}

fn main() -> Result<()> {
  let opt = Opt::from_args();
  log_init(opt.quiet, opt.verbose)?;

  let inf = opt.infile.as_path();
  let fs = fs::File::open(inf)?;
  let fs = BufReader::new(fs);
  let mut zf = ZipArchive::new(fs)?;
  if zf.len() > 1 {
    error!("{:?}: more than one member file", inf);
    return Err(bookdata::err("too many input files"))
  } else if zf.len() == 0 {
    error!("{:?}: empty input archive", inf);
    return Err(bookdata::err("empty input archive"));
  }
  let member = zf.by_index(0)?;
  info!("processing member {:?} with {} bytes", member.name(), member.size());
  
  let outp = opt.outdir.as_path();
  if !outp.is_dir() {
    fs::create_dir_all(&outp)?;
  }

  let node_out = open_out(&outp, "nodes.snappy")?;
  let lit_out = open_out(&outp, "literals.snappy")?;
  let mut triples_out = open_out(&outp, "triples.snappy")?;

  let mut nodes = NodeIndex::create(node_out, member.name());
  let mut lits = LitWriter::create(lit_out);

  let db = bookdata::db::db_open(&opt.db_url)?;
  nodes.load(&db, &opt)?;
  lits.load(&db, &opt)?;
  info!("database has {} nodes", nodes.table.len());

  let pb = ProgressBar::new(member.size());
  pb.set_style(ProgressStyle::default_bar().template("{elapsed_precise} {bar} {percent}% {bytes}/{total_bytes} (eta: {eta})"));
  let pbr = pb.wrap_read(member);
  let pbr = BufReader::new(pbr);
  let mut lno = 0;
  for line in pbr.lines() {
    let line = line?;
    lno += 1;
    match triple_line(&line) {
      Ok(Some(tr)) => {
        let s_id = nodes.subj_id(&tr.subject)?;
        let p_id = nodes.pred_id(&tr.predicate)?;
        let o_id = obj_id(&mut nodes, &mut lits, &tr.object)?;
        write!(&mut triples_out, "{}\t{}\t{}\n", s_id, p_id, o_id)?
      },
      Ok(None) => (),
      Err(e) => {
        pb.println(format!("error on line {}: {:?}", lno, e));
        error!("invalid line contained: {}", line);
      }
    };
  }

  Ok(())
}
