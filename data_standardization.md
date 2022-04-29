# Data standardization for biotrainer

In this document we collect different data standardization strategies for the various prediction tasks.
We will list all possibilities we considered and justify the final decision we made.

## Standardization decisions

### Residue -> Class
**2 Fasta files (sequence.fasta, label.fasta)**
```fasta
# sequences.fasta
>Seq1
SEQWENCE
# labels.fasta
>Seq1 SET=train VALIDATION=False
DVCDVVDD
```

### Sequence -> Class && Sequence -> Value
**1 single Fasta file**
```fasta
# sequences.fasta
>Seq1 Label=Glob SET=train VALIDATION=False 
SEQWENCE
```

## Standardization strategies overview

### Residue -> Class
<details>
<summary>Residue -> Class standardisation</summary>

**1. 2 Fasta files (sequence.fasta, label.fasta)**
```fasta
# sequences.fasta
>Seq1
SEQWENCE
# labels.fasta
>Seq1 SET=train VALIDATION=False
DVCDVVDD
```

PRO:
+ Easy mapping of residue -> class

</details>

### Residue -> Value
<details>
<summary>Residue -> Value standardisation</summary>

**1. 1 single CSV file**
```csv
sequence, values, set, validation
PRTEIN, 0.5;0.3;0.2;0.1;1.5;0.01, train, False
```

PRO:
+ Only one file

CON:
- File will be very large and have bad readability

</details>

### Sequence -> Class && Sequence -> Value
<details>
<summary>Sequence -> Class standardisation</summary>

**1. 2 Fasta files (sequence.fasta, label.fasta)**
```fasta
# sequences.fasta
>Seq1
SEQWENCE
# labels.fasta
>Seq1 SET=train VALIDATION=False
Glob
```

PRO:
+ Compliant with residue -> class structure

CON:
- Fasta interpreters might misinterpret "Glob" as "G, L, O, B"
- 2 files

**2. 1 single Fasta file**
```fasta
# sequences.fasta
>Seq1 Label=Glob SET=train VALIDATION=False 
SEQWENCE
```

PRO:
+ Only one file
+ Readability

CON:
- Conversion from FLIP to biotrainer needed

**3. 1 single CSV file**
```csv
sequence, label, set, validation
SEQWENCE, Glob, train, False
```

PRO:
+ Only one file
+ FLIP data format

CON:
- Bad readability for longer sequences
</details>